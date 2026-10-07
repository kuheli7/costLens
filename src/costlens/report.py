"""Turn raw cost rows into a verified DayReport: the single source of every reported number."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction

from .attribution import (
    CauseNode,
    Table,
    baseline_table,
    drill_down,
    table_from_rows,
    usage_rows,
)
from .baseline import (
    DEFAULT_CONFIG,
    Anomaly,
    AnomalyConfig,
    compute_baseline,
    daily_series,
    find_anomalies,
)
from .cloudtrail import ContextMatch, match_cause, window_for_day
from .dataquality import (
    Reconciliation,
    Untagged,
    non_usage_cents,
    provisional_warning,
    reconcile,
    untagged_spend,
)
from .models import DIMENSIONS, CostRow
from .money import round_half_up
from .waste.rules import Finding

MAX_CAUSES = 3
MAX_ONSET_LOOKBACK_DAYS = 6


@dataclass(frozen=True, slots=True)
class CauseReport:
    rank: int
    scope: dict[str, str]
    delta_cents: int
    share: float
    before_cents: int
    after_cents: int
    volume_cents: int | None
    rate_cents: int | None
    qty_before: float | None
    qty_after: float | None
    contributors: tuple[str, ...]  # resource ids behind a group-level cause
    onset: date
    match: ContextMatch | None

    @property
    def service(self) -> str:
        return self.scope.get("service", "")

    @property
    def resource_id(self) -> str:
        return self.scope.get("resource_id", "")


@dataclass(slots=True)
class DayReport:
    day: date
    total_cents: int
    baseline_cents: int
    delta_cents: int
    pct: float | None
    baseline_method: str
    baseline_days: tuple[date, ...]
    significant: bool
    anomalies: list[Anomaly]
    causes: list[CauseReport]
    untagged: Untagged
    non_usage: dict[str, int]
    waste: list[Finding] = field(default_factory=list)
    reconciliation: Reconciliation | None = None
    warnings: list[str] = field(default_factory=list)


def _day_table(rows_by_day: dict[date, list[CostRow]], d: date) -> Table:
    return table_from_rows(rows_by_day.get(d, []))


def _scope_cost(table: Table, scope: dict[str, str]) -> Fraction:
    idx = [(DIMENSIONS.index(d), v) for d, v in scope.items()]
    return sum((c.cost for k, c in table.items() if all(k[i] == v for i, v in idx)), Fraction(0))


def onset_day(
    rows_by_day: dict[date, list[CostRow]],
    scope: dict[str, str],
    day: date,
    before_cents: int,
    delta_cents: int,
) -> date:
    """First day of the current run of elevated spend inside `scope` (looking back a week)."""
    onset = day
    threshold = before_cents + delta_cents / 2
    for k in range(1, MAX_ONSET_LOOKBACK_DAYS + 1):
        d = day - timedelta(days=k)
        if d not in rows_by_day:
            break
        cost = _scope_cost(_day_table(rows_by_day, d), scope)
        elevated = cost >= threshold if delta_cents > 0 else cost <= threshold
        if not elevated:
            break
        onset = d
    return onset


def _contributors(node: CauseNode) -> tuple[str, ...]:
    return tuple(c.scope["resource_id"] for c in node.children if c.scope.get("resource_id"))


def analyze_day(
    rows: Sequence[CostRow],
    day: date,
    cloudtrail_events: Sequence[dict] = (),
    waste: Sequence[Finding] = (),
    now: datetime | None = None,
    ce_total_cents: int | None = None,
    cfg: AnomalyConfig = DEFAULT_CONFIG,
    max_causes: int = MAX_CAUSES,
) -> DayReport:
    """Compare `day` with its baseline and explain the change.

    `rows` should hold the days up to and including `day` (later days are ignored).
    """
    now = now or datetime.now(UTC)
    rows = [r for r in rows if r.day <= day]
    day_rows = [r for r in rows if r.day == day]
    usage = usage_rows(r for r in rows if r.line_type != "Fee")
    rows_by_day: dict[date, list[CostRow]] = {}
    for r in usage:
        rows_by_day.setdefault(r.day, []).append(r)

    series = daily_series(rows)
    base = compute_baseline(series, day, cfg)
    warnings: list[str] = []
    if base is None:
        warnings.append("not enough history to compare this day with a baseline")
        base_days: tuple[date, ...] = ()
        before: Table = {}
        method = "none"
    else:
        base_days = base.comparison_days
        method = base.method
        before = (
            baseline_table([_day_table(rows_by_day, d) for d in base_days]) if base_days else {}
        )
        if method == "trailing":
            warnings.append(
                "baseline uses the trailing 7 days because same-weekday history is short"
            )
        if method == "new":
            warnings.append("no earlier data: every cost counts as new")

    after = _day_table(rows_by_day, day)
    root = drill_down(before, after)
    total = sum(c.cost for c in after.values())
    before_total = sum(c.cost for c in before.values())
    delta = root.delta_cents
    pct = None if before_total == 0 else float(Fraction(delta) / before_total)

    anomalies = find_anomalies(rows, day, cfg)
    significant = bool(anomalies)

    causes: list[CauseReport] = []
    if significant and delta != 0:
        for rank, node in enumerate(root.causes()[:max_causes], start=1):
            onset = onset_day(rows_by_day, node.scope, day, node.before_cents, node.delta_cents)
            contributors = _contributors(node)
            res = (
                [node.scope["resource_id"]] if node.scope.get("resource_id") else list(contributors)
            )
            if not res:
                res = sorted(
                    {
                        k[DIMENSIONS.index("resource_id")]
                        for k in after
                        if all(k[DIMENSIONS.index(d)] == v for d, v in node.scope.items())
                    }
                )
            match = match_cause(
                node.scope.get("service", ""),
                res,
                node.scope.get("asg", ""),
                window_for_day(onset),
                cloudtrail_events,
            )
            sp = node.split
            causes.append(
                CauseReport(
                    rank,
                    dict(node.scope),
                    node.delta_cents,
                    node.share,
                    node.before_cents,
                    node.after_cents,
                    sp.volume_cents if sp else None,
                    sp.rate_cents if sp else None,
                    sp.qty_before if sp else None,
                    sp.qty_after if sp else None,
                    contributors,
                    onset,
                    match,
                )
            )

    recon = None
    if ce_total_cents is not None:
        recon = reconcile(sum(r.cost_cents for r in day_rows), ce_total_cents)
        if not recon.ok and recon.message:
            warnings.append(recon.message)
    prov = provisional_warning(day, now)
    if prov:
        warnings.append(prov)
    fees = sum(r.cost_cents for r in day_rows if r.line_type == "Fee")
    if fees:
        warnings.append("monthly fee lines are excluded from the comparison")

    return DayReport(
        day=day,
        total_cents=round_half_up(total),
        baseline_cents=round_half_up(total) - delta,  # reconciles with the exact change
        delta_cents=delta,
        pct=pct,
        baseline_method=method,
        baseline_days=base_days,
        significant=significant,
        anomalies=anomalies,
        causes=causes,
        untagged=untagged_spend(day_rows),
        non_usage=non_usage_cents(day_rows),
        waste=list(waste),
        reconciliation=recon,
        warnings=warnings,
    )
