"""Where a finding's savings number comes from.

Savings are taken from what the account actually paid for that exact resource (CUR unit
price x usage). Only when the CUR has no line for the resource do we fall back to a rough
list-price estimate, and the finding is then labelled ``estimate`` so nobody mistakes it
for a measured figure.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from ..models import CostRow

DAYS_PER_MONTH = 30

# FALLBACK ESTIMATES: approximate on-demand list prices in US cents, unverified against the
# AWS Pricing API for any region. Used only when the CUR has no cost line for a resource.
ESTIMATE_EBS_GB_MONTH_CENTS = 8.0
ESTIMATE_EIP_IDLE_MONTH_CENTS = 365
ESTIMATE_NAT_MONTH_CENTS = 4_100
ESTIMATE_LB_MONTH_CENTS = 1_650
ESTIMATE_INSTANCE_MONTH_CENTS = {
    "t3.micro": 760,
    "t3.small": 1_520,
    "t3.medium": 3_040,
    "t3.large": 6_080,
    "m5.large": 7_000,
    "m5.xlarge": 14_000,
    "m5.2xlarge": 28_000,
    "m5.4xlarge": 56_000,
}


@dataclass(frozen=True, slots=True)
class Cost:
    monthly_cents: int
    source: str  # "cur" | "estimate" | "unknown"


class PriceBook:
    """Monthly cost per resource, learned from recent cost rows."""

    def __init__(self, rows: Iterable[CostRow] = (), window_days: int = 7):
        by_resource: dict[str, int] = defaultdict(int)
        days: set = set()
        for r in rows:
            if r.resource_id and r.line_type == "Usage":
                by_resource[r.resource_id] += r.cost_cents
                days.add(r.day)
        n = max(1, min(window_days, len(days)))
        self._monthly = {k: round(v / n * DAYS_PER_MONTH) for k, v in by_resource.items()}

    def monthly(self, resource_id: str, fallback_cents: float | None) -> Cost:
        if resource_id in self._monthly:
            return Cost(self._monthly[resource_id], "cur")
        if fallback_cents is None:
            return Cost(0, "unknown")
        return Cost(round(fallback_cents), "estimate")

    def instance(self, instance_id: str, instance_type: str) -> Cost:
        return self.monthly(instance_id, ESTIMATE_INSTANCE_MONTH_CENTS.get(instance_type))

    def volume(self, volume_id: str, size_gb: int) -> Cost:
        return self.monthly(volume_id, size_gb * ESTIMATE_EBS_GB_MONTH_CENTS)
