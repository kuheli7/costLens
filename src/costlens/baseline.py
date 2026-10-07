"""Baselines and anomaly scoring on daily cost series.

Baseline = median of the same weekday over the last four weeks (weekends differ), falling
back to a trailing 7-day median when there is too little same-weekday history. The score is a
robust z-score (deviation over the median absolute deviation), so one past spike does not
distort the baseline. An alert needs a large change in relative AND absolute terms.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from .models import NON_USAGE_LINE_TYPES, CostRow

MAD_SCALE = 1.4826  # makes MAD comparable to a standard deviation for normal data


@dataclass(frozen=True, slots=True)
class AnomalyConfig:
    min_pct: float = 0.25  # relative change that counts as large
    min_abs_cents: int = 500  # absolute change that counts as large ($5/day)
    min_z: float = 3.0  # robust z-score
    weekday_weeks: int = 4
    min_same_weekday_points: int = 3
    trailing_days: int = 7
    min_trailing_points: int = 3
    mad_floor_pct: float = 0.02  # never divide by less than 2% of the median ...
    mad_floor_cents: float = 25.0  # ... or 25 cents


DEFAULT_CONFIG = AnomalyConfig()


@dataclass(frozen=True, slots=True)
class Baseline:
    expected_cents: float
    mad_cents: float  # scaled and floored
    method: str  # same_weekday | trailing | new
    n_points: int
    comparison_days: tuple[date, ...]  # days the baseline was computed from


def daily_series(rows: Iterable[CostRow], exclude_fees: bool = True) -> dict[date, int]:
    """Usage cost per day. Tax, credits, refunds and (by default) monthly fees are excluded."""
    out: dict[date, int] = defaultdict(int)
    for r in rows:
        if r.line_type in NON_USAGE_LINE_TYPES or (exclude_fees and r.line_type == "Fee"):
            continue
        out[r.day] += r.cost_cents
    return dict(out)


def series_by(rows: Sequence[CostRow], dim: str) -> dict[str, dict[date, int]]:
    """Daily usage cost per value of a dimension (e.g. per service)."""
    groups: dict[str, list[CostRow]] = defaultdict(list)
    for r in rows:
        groups[r.dim(dim)].append(r)
    return {k: daily_series(v) for k, v in groups.items()}


def compute_baseline(
    series: dict[date, int],
    day: date,
    cfg: AnomalyConfig = DEFAULT_CONFIG,
    short_history_is_new: bool = False,
) -> Baseline | None:
    """Baseline for `day` from the days before it.

    Returns None when there is too little history to judge. With `short_history_is_new`
    (used for per-service series) a subject first seen fewer than `min_trailing_points` days ago
    is treated as new, with an expected spend of zero, so a service that appeared two days ago
    is still reported instead of silently skipped.
    """
    history = [d for d in series if d < day]
    if not history:
        return Baseline(0.0, cfg.mad_floor_cents, "new", 0, ())
    first_seen = min(history)

    def value(d: date) -> int | None:
        if d < first_seen:
            return None  # before the subject existed; says nothing about its normal level
        return series.get(d, 0)  # a missing day after first sight is a zero-cost day

    same = [
        (d, v)
        for k in range(1, cfg.weekday_weeks + 1)
        if (d := day - timedelta(days=7 * k)) and (v := value(d)) is not None
    ]
    method = "same_weekday"
    if len(same) < cfg.min_same_weekday_points:
        same = [
            (d, v)
            for k in range(1, cfg.trailing_days + 1)
            if (d := day - timedelta(days=k)) and (v := value(d)) is not None
        ]
        method = "trailing"
        if len(same) < cfg.min_trailing_points:
            if short_history_is_new and (day - first_seen).days < cfg.min_trailing_points:
                return Baseline(0.0, cfg.mad_floor_cents, "new", 0, ())
            return None
    values = [v for _, v in same]
    med = float(median(values))
    mad = float(median(abs(v - med) for v in values)) * MAD_SCALE
    mad = max(mad, cfg.mad_floor_pct * abs(med), cfg.mad_floor_cents)
    return Baseline(med, mad, method, len(values), tuple(d for d, _ in same))


@dataclass(frozen=True, slots=True)
class Anomaly:
    subject: str  # "total" or a service name
    day: date
    actual_cents: int
    expected_cents: int
    delta_cents: int
    pct: float | None  # None for a subject with no baseline (new)
    z: float
    method: str


def check_day(
    subject: str,
    series: dict[date, int],
    day: date,
    cfg: AnomalyConfig = DEFAULT_CONFIG,
    short_history_is_new: bool = False,
) -> Anomaly | None:
    """Return an Anomaly if `day` is out of range for this series, else None."""
    base = compute_baseline(series, day, cfg, short_history_is_new)
    if base is None:
        return None
    actual = series.get(day, 0)
    delta = actual - base.expected_cents
    z = delta / base.mad_cents
    pct = None if base.expected_cents <= 0 else delta / base.expected_cents
    large_rel = pct is None or abs(pct) >= cfg.min_pct
    if not (large_rel and abs(delta) >= cfg.min_abs_cents and abs(z) >= cfg.min_z):
        return None
    return Anomaly(
        subject, day, actual, round(base.expected_cents), round(delta), pct, z, base.method
    )


def find_anomalies(
    rows: Sequence[CostRow], day: date, cfg: AnomalyConfig = DEFAULT_CONFIG
) -> list[Anomaly]:
    """Anomalies for the total and for each service on `day`, biggest first."""
    out: list[Anomaly] = []
    total = check_day("total", daily_series(rows), day, cfg)
    if total:
        out.append(total)
    for service, series in series_by(rows, "service").items():
        a = check_day(service, series, day, cfg, short_history_is_new=True)
        if a:
            out.append(a)
    return sorted(out, key=lambda a: -abs(a.delta_cents))
