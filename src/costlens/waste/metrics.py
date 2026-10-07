"""CloudWatch metric access behind a small interface so rules can be tested with fixtures."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol


class MetricsProvider(Protocol):
    def series(
        self,
        namespace: str,
        metric: str,
        dimensions: dict[str, str],
        stat: str,
        period: int,
        days: int,
        now: datetime,
    ) -> list[float]:
        """Datapoints oldest first. Empty when CloudWatch has nothing for the metric."""
        ...


class CloudWatchMetrics:
    """MetricsProvider backed by cloudwatch:GetMetricData (read-only)."""

    def __init__(self, client: Any):
        self._cw = client

    def series(self, namespace, metric, dimensions, stat, period, days, now):
        query = {
            "Id": "m1",
            "MetricStat": {
                "Metric": {
                    "Namespace": namespace,
                    "MetricName": metric,
                    "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()],
                },
                "Period": period,
                "Stat": stat,
            },
            "ReturnData": True,
        }
        values: list[tuple[datetime, float]] = []
        token = None
        while True:
            kwargs = {
                "MetricDataQueries": [query],
                "StartTime": now - timedelta(days=days),
                "EndTime": now,
                "ScanBy": "TimestampAscending",
            }
            if token:
                kwargs["NextToken"] = token
            resp = self._cw.get_metric_data(**kwargs)
            for r in resp["MetricDataResults"]:
                values.extend(zip(r["Timestamps"], r["Values"], strict=True))
            token = resp.get("NextToken")
            if not token:
                break
        return [v for _, v in sorted(values)]


class FixtureMetrics:
    """Metrics from a dict or function, for tests and recorded fixtures.

    Keys are (namespace, metric, sorted-dimension-tuple, stat).
    """

    def __init__(self, data: dict | Callable[..., list[float]] | None = None):
        self._data = data or {}

    def set(self, namespace, metric, dimensions, stat, values):
        self._data[(namespace, metric, tuple(sorted(dimensions.items())), stat)] = list(values)

    def series(self, namespace, metric, dimensions, stat, period, days, now):
        if callable(self._data):
            return self._data(namespace, metric, dimensions, stat, period, days, now)
        return list(
            self._data.get((namespace, metric, tuple(sorted(dimensions.items())), stat), [])
        )
