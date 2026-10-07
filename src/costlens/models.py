"""Core data types shared by every layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Dimensions a cost change can be broken down by, coarsest first. The order is the
# tie-break when two dimensions explain the change equally well.
DIMENSIONS = ("service", "region", "usage_type", "team", "asg", "resource_id")

# Line types that are not usage. They are excluded from root-cause analysis and reported
# separately, so a refund or a tax change is never blamed on an engineer's instance.
NON_USAGE_LINE_TYPES = frozenset({"Tax", "Credit", "Refund"})


@dataclass(frozen=True, slots=True)
class CostRow:
    """One cost line at (day, service, region, usage type, resource) granularity."""

    day: date
    service: str
    region: str
    usage_type: str
    resource_id: str  # "" when the line has no resource (e.g. support)
    team: str  # value of the team tag, "" when untagged
    asg: str  # value of the aws:autoscaling:groupName tag, "" when none
    usage_qty: float
    cost_cents: int
    line_type: str = "Usage"  # Usage | Tax | Credit | Refund | Fee

    def dim(self, name: str) -> str:
        return getattr(self, name)

    @property
    def key(self) -> tuple[str, str, str, str, str, str]:
        return (self.service, self.region, self.usage_type, self.team, self.asg, self.resource_id)
