"""Six waste rules. Each is a small function returning findings with evidence and a fix.

Design rules that apply to every check:

* Read-only. A finding suggests; a human acts. Fix commands are text, never executed.
* Respect intent. ``costlens:ignore=true`` (with ``costlens:ignore-until=YYYY-MM-DD``)
  suppresses a finding until the date passes.
* Avoid false alarms. Resources younger than the lookback window are skipped, and a
  workload that is busy a little every day is treated as batch, not idle.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from .inventory import Inventory
from .metrics import MetricsProvider
from .prices import (
    ESTIMATE_EIP_IDLE_MONTH_CENTS,
    ESTIMATE_LB_MONTH_CENTS,
    ESTIMATE_NAT_MONTH_CENTS,
    PriceBook,
)

IGNORE_TAG = "costlens:ignore"
IGNORE_UNTIL_TAG = "costlens:ignore-until"


@dataclass(frozen=True, slots=True)
class WasteConfig:
    """Thresholds. Every default here is a starting point; all are configurable."""

    idle_cpu_p95_pct: float = 5.0
    idle_network_mb_per_day: float = 5.0
    ec2_lookback_days: int = 14
    batch_busy_cpu_pct: float = 20.0  # a datapoint above this counts as "busy"
    batch_min_busy_hours: int = 10  # busy hours recurring this often means batch, not idle
    asg_low_cpu_pct: float = 20.0
    asg_target_cpu_pct: float = 40.0
    asg_lookback_days: int = 7
    ebs_unattached_days: int = 7
    stopped_days: int = 14
    nat_lookback_days: int = 7
    nat_idle_bytes_per_day: float = 1_000_000
    lb_idle_requests_per_week: float = 100
    allow_list: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    resource_id: str
    region: str
    evidence: dict[str, Any]
    monthly_savings_cents: int
    savings_source: str  # cur | estimate | unknown
    fix: str
    summary: str


@dataclass(frozen=True, slots=True)
class Suppressed:
    rule: str
    resource_id: str
    reason: str


@dataclass(slots=True)
class Ctx:
    inv: Inventory
    metrics: MetricsProvider
    prices: PriceBook
    cfg: WasteConfig
    now: datetime
    findings: list[Finding] = field(default_factory=list)
    suppressed: list[Suppressed] = field(default_factory=list)

    def skip(self, rule: str, rid: str, tags: dict[str, str], reason: str | None = None) -> bool:
        """True (and recorded) when the resource must not produce a finding."""
        if reason:
            self.suppressed.append(Suppressed(rule, rid, reason))
            return True
        if rid in self.cfg.allow_list:
            self.suppressed.append(Suppressed(rule, rid, "on the allow-list"))
            return True
        if tags.get(IGNORE_TAG, "").lower() == "true":
            until = _parse_date(tags.get(IGNORE_UNTIL_TAG, ""))
            if until is None:
                self.suppressed.append(
                    Suppressed(
                        rule, rid, "ignored by tag with no expiry; set costlens:ignore-until"
                    )
                )
                return True
            if self.now.date() <= until:
                self.suppressed.append(Suppressed(rule, rid, f"ignored by tag until {until}"))
                return True
        return False

    def add(self, **kw: Any) -> None:
        self.findings.append(Finding(region=self.inv.region, **kw))


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        return None


def _age_days(since: datetime, now: datetime) -> float:
    return (now - since).total_seconds() / 86400


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile (p in 0..100)."""
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


# --------------------------------------------------------------------------- rules


def idle_ec2(ctx: Ctx) -> None:
    rule, cfg = "idle_ec2", ctx.cfg
    for i in ctx.inv.instances:
        if i.state != "running" or i.asg:
            continue  # auto scaling members are judged by the group rule
        if ctx.skip(rule, i.id, i.tags):
            continue
        if _age_days(i.launched, ctx.now) < cfg.ec2_lookback_days:
            ctx.skip(rule, i.id, i.tags, f"younger than the {cfg.ec2_lookback_days}-day lookback")
            continue
        dims = {"InstanceId": i.id}
        cpu = ctx.metrics.series(
            "AWS/EC2", "CPUUtilization", dims, "Average", 3600, cfg.ec2_lookback_days, ctx.now
        )
        if len(cpu) < cfg.ec2_lookback_days * 24 * 0.5:
            ctx.skip(rule, i.id, i.tags, "not enough CloudWatch data to judge")
            continue
        p95 = percentile(cpu, 95)
        if p95 >= cfg.idle_cpu_p95_pct:
            continue
        busy_hours = sum(1 for v in cpu if v > cfg.batch_busy_cpu_pct)
        if busy_hours >= cfg.batch_min_busy_hours:
            ctx.skip(rule, i.id, i.tags, "looks like a batch workload (busy hours recur)")
            continue
        net = 0.0
        for metric in ("NetworkIn", "NetworkOut"):
            days = ctx.metrics.series(
                "AWS/EC2", metric, dims, "Sum", 86400, cfg.ec2_lookback_days, ctx.now
            )
            net += sum(days) / max(1, len(days))
        net_mb = net / 1_000_000
        if net_mb >= cfg.idle_network_mb_per_day:
            continue
        cost = ctx.prices.instance(i.id, i.instance_type)
        ctx.add(
            rule=rule,
            resource_id=i.id,
            evidence={
                "p95_cpu_pct": round(p95, 2),
                "network_mb_per_day": round(net_mb, 2),
                "lookback_days": cfg.ec2_lookback_days,
                "instance_type": i.instance_type,
            },
            monthly_savings_cents=cost.monthly_cents,
            savings_source=cost.source,
            fix=f"aws ec2 stop-instances --instance-ids {i.id}",
            summary=f"{i.id} ({i.instance_type}) is idle: p95 CPU {p95:.1f}%, "
            f"{net_mb:.1f} MB/day of network over {cfg.ec2_lookback_days} days",
        )


def oversized_asg(ctx: Ctx) -> None:
    rule, cfg = "oversized_asg", ctx.cfg
    by_id = {i.id: i for i in ctx.inv.instances}
    for g in ctx.inv.asgs:
        if ctx.skip(rule, g.name, g.tags):
            continue
        cpu = ctx.metrics.series(
            "AWS/EC2",
            "CPUUtilization",
            {"AutoScalingGroupName": g.name},
            "Average",
            3600,
            cfg.asg_lookback_days,
            ctx.now,
        )
        if len(cpu) < cfg.asg_lookback_days * 24 * 0.5:
            ctx.skip(rule, g.name, g.tags, "not enough CloudWatch data to judge")
            continue
        avg = sum(cpu) / len(cpu)
        desired_series = ctx.metrics.series(
            "AWS/AutoScaling",
            "GroupDesiredCapacity",
            {"AutoScalingGroupName": g.name},
            "Average",
            3600,
            cfg.asg_lookback_days,
            ctx.now,
        )
        never_scales_in = (
            len(desired_series) >= cfg.asg_lookback_days * 24 * 0.5
            and min(desired_series) == max(desired_series)
            and g.desired >= 2 * g.min_size
        )
        low_cpu = avg < cfg.asg_low_cpu_pct and g.min_size > 1
        if not (low_cpu or (never_scales_in and avg < cfg.asg_target_cpu_pct)):
            continue
        suggested = max(1, math.ceil(g.desired * avg / cfg.asg_target_cpu_pct))
        surplus = g.desired - suggested
        if surplus <= 0:
            continue
        members = [by_id[i] for i in g.instance_ids if i in by_id]
        costs = [ctx.prices.instance(m.id, m.instance_type) for m in members]
        if costs:
            per_instance = sum(c.monthly_cents for c in costs) / len(costs)
            source = "cur" if all(c.source == "cur" for c in costs) else "estimate"
        else:
            per_instance, source = 0.0, "unknown"
        new_min = min(g.min_size, suggested)
        if never_scales_in:
            fix = (
                "fix the scale-in policy or cooldown (e.g. aws autoscaling put-scaling-policy ...)"
            )
        else:
            fix = (
                f"aws autoscaling update-auto-scaling-group --auto-scaling-group-name {g.name} "
                f"--min-size {new_min}"
            )
        ctx.add(
            rule=rule,
            resource_id=g.name,
            evidence={
                "avg_cpu_pct": round(avg, 2),
                "min_size": g.min_size,
                "desired": g.desired,
                "never_scales_in": never_scales_in,
                "suggested_capacity": suggested,
                "lookback_days": cfg.asg_lookback_days,
            },
            monthly_savings_cents=round(surplus * per_instance),
            savings_source=source,
            fix=fix,
            summary=f"{g.name} runs {g.desired} instances at {avg:.0f}% average CPU; "
            f"about {suggested} would do",
        )


def unattached_ebs(ctx: Ctx) -> None:
    rule, cfg = "unattached_ebs", ctx.cfg
    for v in ctx.inv.volumes:
        if v.state != "available":
            continue
        if ctx.skip(rule, v.id, v.tags):
            continue
        since = ctx.inv.volume_detached_at.get(v.id, v.created)
        age = _age_days(since, ctx.now)
        if age < cfg.ebs_unattached_days:
            ctx.skip(rule, v.id, v.tags, f"unattached for less than {cfg.ebs_unattached_days} days")
            continue
        cost = ctx.prices.volume(v.id, v.size_gb)
        ctx.add(
            rule=rule,
            resource_id=v.id,
            evidence={
                "size_gb": v.size_gb,
                "unattached_days": int(age),
                "age_measured_from": "detach event"
                if v.id in ctx.inv.volume_detached_at
                else "creation",
            },
            monthly_savings_cents=cost.monthly_cents,
            savings_source=cost.source,
            fix=f"aws ec2 create-snapshot --volume-id {v.id}  # then, once complete: "
            f"aws ec2 delete-volume --volume-id {v.id}",
            summary=f"{v.id} ({v.size_gb} GB) has been unattached for {int(age)} days",
        )


def stopped_instance_with_storage(ctx: Ctx) -> None:
    rule, cfg = "stopped_instance_storage", ctx.cfg
    vols = {v.id: v for v in ctx.inv.volumes}
    for i in ctx.inv.instances:
        if i.state != "stopped" or not i.volume_ids:
            continue
        if ctx.skip(rule, i.id, i.tags):
            continue
        if i.stopped_at is None:
            ctx.skip(rule, i.id, i.tags, "stop time unknown")
            continue
        days = _age_days(i.stopped_at, ctx.now)
        if days < cfg.stopped_days:
            ctx.skip(rule, i.id, i.tags, f"stopped for less than {cfg.stopped_days} days")
            continue
        costs = [
            ctx.prices.volume(vid, vols[vid].size_gb if vid in vols else 0) for vid in i.volume_ids
        ]
        total = sum(c.monthly_cents for c in costs)
        source = "cur" if all(c.source == "cur" for c in costs) else "estimate"
        size = sum(vols[v].size_gb for v in i.volume_ids if v in vols)
        snaps = "; ".join(f"aws ec2 create-snapshot --volume-id {v}" for v in i.volume_ids)
        ctx.add(
            rule=rule,
            resource_id=i.id,
            evidence={
                "stopped_days": int(days),
                "volume_ids": list(i.volume_ids),
                "storage_gb": size,
            },
            monthly_savings_cents=total,
            savings_source=source,
            fix=f"{snaps}; then aws ec2 terminate-instances --instance-ids {i.id}",
            summary=f"{i.id} has been stopped {int(days)} days but still pays for {size} GB of EBS",
        )


def unattached_eip(ctx: Ctx) -> None:
    rule = "unattached_eip"
    for a in ctx.inv.addresses:
        if a.associated:
            continue
        if ctx.skip(rule, a.allocation_id, a.tags):
            continue
        cost = ctx.prices.monthly(a.allocation_id, ESTIMATE_EIP_IDLE_MONTH_CENTS)
        ctx.add(
            rule=rule,
            resource_id=a.allocation_id,
            evidence={"public_ip": a.public_ip},
            monthly_savings_cents=cost.monthly_cents,
            savings_source=cost.source,
            fix=f"aws ec2 release-address --allocation-id {a.allocation_id}",
            summary=f"Elastic IP {a.public_ip} is not associated with anything",
        )


def idle_nat_or_lb(ctx: Ctx) -> None:
    rule, cfg = "idle_nat_or_lb", ctx.cfg
    for n in ctx.inv.nat_gateways:
        if n.state != "available" or ctx.skip(rule, n.id, n.tags):
            continue
        if _age_days(n.created, ctx.now) < cfg.nat_lookback_days:
            ctx.skip(rule, n.id, n.tags, f"younger than the {cfg.nat_lookback_days}-day lookback")
            continue
        out = ctx.metrics.series(
            "AWS/NATGateway",
            "BytesOutToDestination",
            {"NatGatewayId": n.id},
            "Sum",
            86400,
            cfg.nat_lookback_days,
            ctx.now,
        )
        if len(out) < cfg.nat_lookback_days * 0.5:
            ctx.skip(rule, n.id, n.tags, "not enough CloudWatch data to judge")
            continue
        per_day = sum(out) / len(out)
        if per_day >= cfg.nat_idle_bytes_per_day:
            continue
        cost = ctx.prices.monthly(n.id, ESTIMATE_NAT_MONTH_CENTS)
        ctx.add(
            rule=rule,
            resource_id=n.id,
            evidence={"bytes_out_per_day": int(per_day), "lookback_days": cfg.nat_lookback_days},
            monthly_savings_cents=cost.monthly_cents,
            savings_source=cost.source,
            fix=f"aws ec2 delete-nat-gateway --nat-gateway-id {n.id}  "
            "# if it carries S3 traffic, add a free S3 gateway endpoint instead",
            summary=f"NAT gateway {n.id} moved {per_day / 1e6:.2f} MB/day over "
            f"{cfg.nat_lookback_days} days",
        )
    for lb in ctx.inv.load_balancers:
        if ctx.skip(rule, lb.name, lb.tags):
            continue
        if _age_days(lb.created, ctx.now) < cfg.nat_lookback_days:
            ctx.skip(
                rule, lb.name, lb.tags, f"younger than the {cfg.nat_lookback_days}-day lookback"
            )
            continue
        metric = "RequestCount" if lb.kind == "application" else "ProcessedBytes"
        ns = "AWS/ApplicationELB" if lb.kind == "application" else "AWS/NetworkELB"
        series = ctx.metrics.series(
            ns,
            metric,
            {"LoadBalancer": lb.metric_dimension},
            "Sum",
            86400,
            cfg.nat_lookback_days,
            ctx.now,
        )
        if len(series) < cfg.nat_lookback_days * 0.5:
            ctx.skip(rule, lb.name, lb.tags, "not enough CloudWatch data to judge")
            continue
        total = sum(series)
        limit = cfg.lb_idle_requests_per_week if lb.kind == "application" else 1_000_000
        if total >= limit:
            continue
        cost = ctx.prices.monthly(lb.name, ESTIMATE_LB_MONTH_CENTS)
        ctx.add(
            rule=rule,
            resource_id=lb.name,
            evidence={metric: int(total), "lookback_days": cfg.nat_lookback_days, "type": lb.kind},
            monthly_savings_cents=cost.monthly_cents,
            savings_source=cost.source,
            fix=f"aws elbv2 delete-load-balancer --load-balancer-arn {lb.arn}",
            summary=f"Load balancer {lb.name} handled {int(total)} {metric} in "
            f"{cfg.nat_lookback_days} days",
        )


RULES: tuple[Callable[[Ctx], None], ...] = (
    idle_ec2,
    oversized_asg,
    unattached_ebs,
    stopped_instance_with_storage,
    unattached_eip,
    idle_nat_or_lb,
)


def run_all(
    inv: Inventory,
    metrics: MetricsProvider,
    prices: PriceBook | None = None,
    cfg: WasteConfig | None = None,
    now: datetime | None = None,
) -> tuple[list[Finding], list[Suppressed]]:
    ctx = Ctx(inv, metrics, prices or PriceBook(), cfg or WasteConfig(), now or datetime.now(UTC))
    for rule in RULES:
        rule(ctx)
    ctx.findings.sort(key=lambda f: -f.monthly_savings_cents)
    return ctx.findings, ctx.suppressed


def total_monthly_savings_cents(findings: list[Finding]) -> int:
    return sum(f.monthly_savings_cents for f in findings)


__all__ = [
    "RULES",
    "Ctx",
    "Finding",
    "Suppressed",
    "WasteConfig",
    "run_all",
    "total_monthly_savings_cents",
]
