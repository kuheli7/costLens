"""Synthetic AWS cost data with injectable, labelled incidents.

Real billing data has no ground truth: you never know *why* spend moved. This generator
builds a plausible account (weekday seasonality, noise, tax, monthly fees, untagged spend)
and can inject one incident of a known kind, recording what the right answer is. The
evaluation harness scores the engine against those labels.

Prices are rough and only need to be self-consistent; they are not a price list.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from .models import CostRow

EC2 = "AmazonEC2"
RDS = "AmazonRDS"
S3 = "AmazonS3"
CLOUDWATCH = "AmazonCloudWatch"
OPENSEARCH = "AmazonES"
SUPPORT = "AWSSupportBusiness"
REGION = "ap-south-1"
ASG_NAME = "api-prod-asg"
NAT_ID = "nat-0f3c9a1b22d4e5f60"
RDS_ID = "db-orders-prod"

INCIDENT_KINDS = (
    "asg_scale_out",
    "new_big_instance",
    "nat_traffic",
    "rate_change",
    "new_service",
    "transfer_spike",
)


@dataclass(frozen=True, slots=True)
class Incident:
    id: str
    kind: str
    start_day: date
    expected: dict[str, str]  # scope entries the top cause must contain
    expected_driver: str | None  # "volume" or "rate" when the split matters
    expected_actor: str | None  # CloudTrail actor a correct explanation should name
    expected_event: str | None


@dataclass(slots=True)
class Dataset:
    rows: list[CostRow]
    cloudtrail: list[dict]
    incidents: list[Incident] = field(default_factory=list)
    first_day: date = date.min
    last_day: date = date.min


def _hex(rng: random.Random, n: int = 17) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _row(
    day, service, usage_type, rid, qty, price, team="backend", asg="", line="Usage", region=REGION
):
    return CostRow(
        day=day,
        service=service,
        region=region,
        usage_type=usage_type,
        resource_id=rid,
        team=team,
        asg=asg,
        usage_qty=qty,
        cost_cents=round(qty * price),
        line_type=line,
    )


class _Account:
    """Stable resources of the fake account, created once per dataset."""

    def __init__(self, rng: random.Random):
        self.asg_instances = [f"i-{_hex(rng)}" for _ in range(3)]
        self.dev_instance = f"i-{_hex(rng)}"
        self.bucket = "arn:aws:s3:::acme-app-assets"
        self.logs = "arn:aws:logs:ap-south-1:123456789012:log-group:/app/api"
        self.analytics_instance = f"i-{_hex(rng)}"  # untagged on purpose


def _weekday_factor(d: date) -> float:
    return {5: 0.7, 6: 0.55}.get(d.weekday(), 1.0)


def _base_rows(d: date, acct: _Account, rng: random.Random, noise: float = 0.03) -> list[CostRow]:
    wf = _weekday_factor(d)

    def noisy(x: float) -> float:
        return max(0.0, x * (1 + rng.gauss(0, noise)))

    rows: list[CostRow] = []
    for iid in acct.asg_instances:
        rows.append(_row(d, EC2, "BoxUsage:m5.large", iid, 24, 10.0, asg=ASG_NAME))
    if d.weekday() < 5:
        rows.append(_row(d, EC2, "BoxUsage:t3.medium", acct.dev_instance, 10, 4.5, team="platform"))
    rows.append(_row(d, EC2, "BoxUsage:t3.large", acct.analytics_instance, 24, 8.3, team=""))
    rows.append(_row(d, EC2, "NatGateway-Hours", NAT_ID, 24, 5.6, team="platform"))
    rows.append(_row(d, EC2, "NatGateway-Bytes", NAT_ID, noisy(150 * wf), 5.6, team="platform"))
    rows.append(
        _row(
            d,
            EC2,
            "DataTransfer-Out-Bytes",
            acct.asg_instances[0],
            noisy(20 * wf),
            9.0,
            asg=ASG_NAME,
        )
    )
    rows.append(_row(d, RDS, "InstanceUsage:db.m5.2xlarge", RDS_ID, 24, 38.0, team="data"))
    rows.append(
        _row(d, S3, "TimedStorage-ByteHrs", acct.bucket, 500 * 24 / 30 / 24, 2.3 * 24, team="web")
    )
    rows.append(_row(d, S3, "Requests-Tier1", acct.bucket, noisy(400_000 * wf), 0.0005, team="web"))
    rows.append(_row(d, CLOUDWATCH, "DataProcessing-Bytes", acct.logs, noisy(4 * wf), 50.0))
    usage_total = sum(r.cost_cents for r in rows)
    rows.append(_row(d, "Tax", "Tax", "", 1, usage_total * 0.18, team="", line="Tax"))
    if d.day == 1:  # monthly support fee: a known quirk, not an incident
        rows.append(_row(d, SUPPORT, "Support", "", 1, 3000, team="", line="Fee"))
    return rows


def _inject(
    kind: str,
    start: date,
    last: date,
    acct: _Account,
    rng: random.Random,
    rows_by_day,
    events,
    scale: float = 1.0,
) -> Incident:
    iid = f"inc-{kind}-{start.isoformat()}"
    days = [start + timedelta(days=i) for i in range((last - start).days + 1)]
    when = datetime.combine(start, time(2, 14), tzinfo=UTC)
    actor = None
    event_name = None

    def add(day, row):
        rows_by_day[day].append(row)

    def bump_tax(day):
        usage = sum(r.cost_cents for r in rows_by_day[day] if r.line_type == "Usage")
        for i, r in enumerate(rows_by_day[day]):
            if r.line_type == "Tax":
                rows_by_day[day][i] = _row(
                    day, "Tax", "Tax", "", 1, usage * 0.18, team="", line="Tax"
                )

    if kind == "asg_scale_out":
        new_ids = [f"i-{_hex(rng)}" for _ in range(max(1, math.ceil(5 * scale)))]
        for d in days:
            for nid in new_ids:
                add(d, _row(d, EC2, "BoxUsage:m5.large", nid, 24, 10.0, asg=ASG_NAME))
        expected = {"service": EC2, "asg": ASG_NAME}
        driver, actor, event_name = "volume", "deploy-bot", "UpdateAutoScalingGroup"
        res = [{"ResourceType": "AWS::AutoScaling::AutoScalingGroup", "ResourceName": ASG_NAME}]
    elif kind == "new_big_instance":
        big = f"i-{_hex(rng)}"
        for d in days:
            add(d, _row(d, EC2, "BoxUsage:m5.4xlarge", big, 24, 80.0 * scale, team="ml"))
        expected = {"service": EC2, "resource_id": big}
        driver, actor, event_name = "volume", "alice", "RunInstances"
        res = [{"ResourceType": "AWS::EC2::Instance", "ResourceName": big}]
    elif kind == "nat_traffic":
        for d in days:
            wf = _weekday_factor(d)
            add(
                d,
                _row(
                    d, EC2, "NatGateway-Bytes", NAT_ID, 150 * wf * 2 * scale, 5.6, team="platform"
                ),
            )
            # same key as the base row: the table builder merges them, tripling the traffic
        expected = {"service": EC2, "resource_id": NAT_ID, "usage_type": "NatGateway-Bytes"}
        driver, actor, event_name = "volume", None, None
        res = []
    elif kind == "rate_change":
        for d in days:
            for i, r in enumerate(rows_by_day[d]):
                if r.resource_id == RDS_ID:
                    rows_by_day[d][i] = _row(
                        d,
                        RDS,
                        r.usage_type,
                        RDS_ID,
                        r.usage_qty,
                        38.0 * (1 + 1.5 * scale),
                        team="data",
                    )
        expected = {"service": RDS, "resource_id": RDS_ID}
        driver, actor, event_name = "rate", None, None
        res = []
    elif kind == "new_service":
        domain = "arn:aws:es:ap-south-1:123456789012:domain/search-prod"
        for d in days:
            add(
                d,
                _row(d, OPENSEARCH, "ESInstance:r5.large", domain, 96 * scale, 14.0, team="search"),
            )
        expected = {"service": OPENSEARCH}
        driver, actor, event_name = "volume", "carol", "CreateElasticsearchDomain"
        res = [{"ResourceType": "AWS::Elasticsearch::Domain", "ResourceName": domain}]
    elif kind == "transfer_spike":
        victim = acct.asg_instances[0]
        for d in days:
            wf = _weekday_factor(d)
            add(
                d,
                _row(
                    d,
                    EC2,
                    "DataTransfer-Out-Bytes",
                    victim,
                    20 * wf * 14 * scale,
                    9.0,
                    asg=ASG_NAME,
                ),
            )
        expected = {"service": EC2, "usage_type": "DataTransfer-Out-Bytes", "resource_id": victim}
        driver, actor, event_name = "volume", None, None
        res = []
    else:  # pragma: no cover - guarded by INCIDENT_KINDS
        raise ValueError(f"unknown incident kind {kind!r}")

    for d in days:
        bump_tax(d)

    if actor:
        events.append(
            {
                "EventId": f"evt-{_hex(rng, 12)}",
                "EventName": event_name,
                "EventTime": when,
                "Username": actor,
                "Resources": res,
            }
        )
    # decoys: an unrelated event just before, and a same-name event on another resource
    events.append(
        {
            "EventId": f"evt-{_hex(rng, 12)}",
            "EventName": "PutParameter",
            "EventTime": when - timedelta(minutes=40),
            "Username": "ci-runner",
            "Resources": [{"ResourceType": "AWS::SSM::Parameter", "ResourceName": "/app/config"}],
        }
    )
    if event_name:
        events.append(
            {
                "EventId": f"evt-{_hex(rng, 12)}",
                "EventName": event_name,
                "EventTime": when + timedelta(hours=1),
                "Username": "bob",
                "Resources": [
                    {
                        "ResourceType": res[0]["ResourceType"],
                        "ResourceName": f"decoy-{_hex(rng, 8)}",
                    }
                ],
            }
        )
    return Incident(iid, kind, start, expected, driver, actor, event_name)


def generate(
    seed: int = 0,
    days: int = 90,
    end: date = date(2026, 10, 6),
    incident: str | Sequence[str] | None = None,
    incident_age: int = 0,
    noise: float = 0.03,
    scale: float = 1.0,
) -> Dataset:
    """Build `days` days of cost data ending on `end`.

    `incident` is one kind or several; each starts `incident_age` days before `end` and persists
    to `end`. `noise` is the relative day-to-day variation of traffic-driven lines and `scale`
    shrinks (<1) or grows (>1) the injected incident, so harder cases can be generated.
    """
    rng = random.Random(seed)
    acct = _Account(rng)
    first = end - timedelta(days=days - 1)
    all_days = [first + timedelta(days=i) for i in range(days)]
    rows_by_day: dict[date, list[CostRow]] = {d: _base_rows(d, acct, rng, noise) for d in all_days}
    events: list[dict] = []
    incidents: list[Incident] = []
    kinds = [incident] if isinstance(incident, str) else list(incident or [])
    for kind in kinds:
        start = end - timedelta(days=incident_age)
        if start.day == 1:  # keep incidents off the monthly-fee day so the labels stay clean
            start += timedelta(days=1) if incident_age > 0 else timedelta(0)
        incidents.append(_inject(kind, start, end, acct, rng, rows_by_day, events, scale))
    rows = [r for d in all_days for r in rows_by_day[d]]
    return Dataset(rows, sorted(events, key=lambda e: e["EventTime"]), incidents, first, end)
