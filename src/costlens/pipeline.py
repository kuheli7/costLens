"""One daily run, end to end: rows in, verified report, explanation, digest, Slack blocks out."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from . import factsheet
from .baseline import DEFAULT_CONFIG, AnomalyConfig
from .explain import Explanation, TokenBudget, Writer, explain
from .models import CostRow
from .report import DayReport, analyze_day
from .slack import alert_note, build_blocks, render_digest
from .state import State
from .waste.rules import Finding


@dataclass(slots=True)
class RunResult:
    report: DayReport
    facts: dict[str, Any]
    explanation: Explanation
    digest_text: str
    blocks: list[dict[str, Any]]
    alert_key: str
    is_alert: bool  # a significant change that has not been acknowledged or snoozed
    note: str | None = None  # why the alert is paused, when it is


def alert_key_for(report: DayReport) -> str:
    """Alerts are keyed by the service behind the biggest cause, so snoozing EC2 spikes does not
    silence an unrelated RDS spike."""
    service = report.causes[0].service if report.causes else ""
    return f"cost:{service or 'total'}"


def run_day(
    rows: Sequence[CostRow],
    day: date,
    *,
    cloudtrail: Sequence[dict] = (),
    waste: Sequence[Finding] = (),
    writer: Writer | None = None,
    cache: dict[str, Explanation] | None = None,
    budget: TokenBudget | None = None,
    ce_total_cents: int | None = None,
    now: datetime | None = None,
    state: State | None = None,
    details_url: str | None = None,
    cfg: AnomalyConfig = DEFAULT_CONFIG,
) -> RunResult:
    report = analyze_day(
        rows, day, cloudtrail, waste, now=now, ce_total_cents=ce_total_cents, cfg=cfg
    )
    facts = factsheet.build(report)
    exp = explain(facts, writer, cache=cache, budget=budget)
    key = alert_key_for(report)
    when = now or _now()
    status = state.alert_status(key, when) if state is not None and report.significant else None
    is_alert = report.significant and status is None
    note = alert_note(status)
    digest = render_digest(facts, exp, note)
    blocks = build_blocks(facts, exp, key, details_url, note)
    if state is not None:
        state.save_day(report, facts, exp, digest, now)
        state.record_findings(waste, day)
    return RunResult(report, facts, exp, digest, blocks, key, is_alert, note)


def _now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
