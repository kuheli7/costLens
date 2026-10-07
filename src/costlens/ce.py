"""Cost Explorer: daily totals for reconciliation only.

Cost Explorer cannot return resource IDs and allows at most two group-bys per call, so it is
not the analysis source (the CUR is). Each request costs about $0.01, so callers fetch one
range per day and cache the result.

Response shape (ResultsByTime -> Groups -> Metrics.UnblendedCost.Amount as a string) is taken
from the AWS API reference and is not yet checked against a live account.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


def _cents(amount: str) -> int:
    return int((Decimal(amount) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def fetch_daily_totals(client: Any, start: date, end_inclusive: date) -> dict[date, int]:
    """Total unblended cost per day in cents, tax and credits included (as billed)."""
    totals: dict[date, int] = {}
    token = None
    while True:
        kwargs: dict[str, Any] = {
            "TimePeriod": {
                "Start": start.isoformat(),
                "End": (end_inclusive + timedelta(days=1)).isoformat(),  # End is exclusive
            },
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
        }
        if token:
            kwargs["NextPageToken"] = token
        resp = client.get_cost_and_usage(**kwargs)
        for period in resp["ResultsByTime"]:
            day = date.fromisoformat(period["TimePeriod"]["Start"])
            totals[day] = _cents(period["Total"]["UnblendedCost"]["Amount"])
        token = resp.get("NextPageToken")
        if not token:
            return totals
