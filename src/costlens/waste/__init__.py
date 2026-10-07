"""Read-only waste detection: six rules over an inventory snapshot and CloudWatch metrics."""

from .inventory import Inventory, collect
from .metrics import CloudWatchMetrics, FixtureMetrics, MetricsProvider
from .prices import PriceBook
from .rules import (
    Finding,
    Suppressed,
    WasteConfig,
    run_all,
    total_monthly_savings_cents,
)

__all__ = [
    "CloudWatchMetrics",
    "Finding",
    "FixtureMetrics",
    "Inventory",
    "MetricsProvider",
    "PriceBook",
    "Suppressed",
    "WasteConfig",
    "collect",
    "run_all",
    "total_monthly_savings_cents",
]
