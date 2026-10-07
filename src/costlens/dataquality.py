"""Data-correctness checks that decide whether a digest can be trusted."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from .models import NON_USAGE_LINE_TYPES, CostRow

CUR_REVISION_WINDOW = timedelta(hours=72)  # CUR data for a day can change for up to 72 hours


def provisional_warning(day: date, now: datetime) -> str | None:
    """Warn when `day` is recent enough that CUR may still revise it."""
    end_of_day = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    if now - end_of_day < CUR_REVISION_WINDOW:
        return "data for this day is provisional and may still be revised"
    return None


@dataclass(frozen=True, slots=True)
class Reconciliation:
    ok: bool
    cur_cents: int
    ce_cents: int
    message: str | None


def reconcile(
    cur_cents: int, ce_cents: int, tolerance_pct: float = 0.02, tolerance_abs_cents: int = 100
) -> Reconciliation:
    """Cost Explorer and CUR totals for the same day must agree within a tolerance.

    Passes if the gap is within `tolerance_pct` of the larger total OR under the absolute
    floor (so cents-level rounding on tiny days never raises an alarm).
    """
    gap = abs(cur_cents - ce_cents)
    limit = max(tolerance_abs_cents, tolerance_pct * max(abs(cur_cents), abs(ce_cents)))
    if gap <= limit:
        return Reconciliation(True, cur_cents, ce_cents, None)
    return Reconciliation(
        False,
        cur_cents,
        ce_cents,
        "cost data is incomplete: Cost Explorer and the Cost and Usage Report disagree",
    )


@dataclass(frozen=True, slots=True)
class Untagged:
    cents: int
    share: float
    resources: tuple[str, ...]


def untagged_spend(rows: Iterable[CostRow], tag_name: str = "team") -> Untagged:
    """Usage spend with an empty `team` tag, with the resources responsible."""
    usage = [r for r in rows if r.line_type == "Usage"]
    total = sum(r.cost_cents for r in usage)
    missing = [r for r in usage if not getattr(r, tag_name)]
    cents = sum(r.cost_cents for r in missing)
    resources = sorted({r.resource_id for r in missing if r.resource_id})
    return Untagged(cents, cents / total if total else 0.0, tuple(resources))


def non_usage_cents(rows: Iterable[CostRow]) -> dict[str, int]:
    """Totals of tax, credits and refunds, so they are reported rather than blamed."""
    out: dict[str, int] = {}
    for r in rows:
        if r.line_type in NON_USAGE_LINE_TYPES:
            out[r.line_type] = out.get(r.line_type, 0) + r.cost_cents
    return out
