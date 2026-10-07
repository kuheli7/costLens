"""Fact sheet: the only thing the language model is allowed to see.

Every figure is pre-formatted text computed by the deterministic engine, so the model has
nothing to calculate. Aggregated facts only: no raw CloudTrail logs, no account IDs, and no
tags beyond the team name.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .money import fmt_usd
from .report import CauseReport, DayReport

SCHEMA_VERSION = 1


def _pct(x: float | None) -> str | None:
    return None if x is None else f"{abs(x) * 100:.0f}%"


def _direction(cents: int) -> str:
    return "up" if cents > 0 else "down" if cents < 0 else "flat"


def fix_hint(c: CauseReport) -> str:
    """A deterministic suggestion for the cause, so the model never invents remedies."""
    ut = c.scope.get("usage_type", "")
    svc = c.service
    if c.rate_cents and abs(c.rate_cents) > abs(c.volume_cents or 0):
        return (
            "the price per unit rose, not the usage; check whether a Savings Plan, "
            "reservation or discount ended, or the instance class changed"
        )
    if "NatGateway-Bytes" in ut:
        return (
            "find the workload sending traffic through the NAT gateway; "
            "traffic to S3 can use a free S3 gateway endpoint instead"
        )
    if "DataTransfer" in ut:
        return "find what is sending the extra data out and whether it can stay inside the region"
    if c.scope.get("asg") and c.delta_cents > 0:
        return (
            "check why the group scaled out and whether its scale-in policy "
            "and cooldown let it shrink"
        )
    if svc == "AmazonEC2" and "BoxUsage" in ut:
        return "confirm the instance is needed; stop or downsize it if not"
    if c.delta_cents < 0:
        return "no action needed; confirm the drop is expected"
    return "confirm this new spend is expected and has an owner"


def describe(c: CauseReport) -> str:
    parts = [c.service or "spend"]
    if c.scope.get("usage_type"):
        parts.append(f"usage type {c.scope['usage_type']}")
    if c.scope.get("asg"):
        parts.append(f"in Auto Scaling group {c.scope['asg']}")
    if c.resource_id:
        parts.append(f"resource {c.resource_id}")
    if c.scope.get("team"):
        parts.append(f"team {c.scope['team']}")
    return ", ".join(parts)


def _cause_facts(c: CauseReport) -> dict[str, Any]:
    f: dict[str, Any] = {
        "id": f"c{c.rank}",
        "what": describe(c),
        "change": fmt_usd(c.delta_cents, sign=True),
        "share_of_total_change": _pct(c.share),
        "direction": _direction(c.delta_cents),
        "started": c.onset.isoformat(),
        "fix_hint": fix_hint(c),
    }
    if c.contributors:
        f["top_resources"] = list(c.contributors)
    if c.volume_cents is not None and c.rate_cents is not None:
        f["volume_effect"] = fmt_usd(c.volume_cents, sign=True)
        f["rate_effect"] = fmt_usd(c.rate_cents, sign=True)
    if c.match:
        f["context"] = {
            "event": c.match.event_name,
            "actor": c.match.actor,
            "time_utc": c.match.time.strftime("%H:%M"),
            "confidence": "exact resource match"
            if c.match.confidence == "exact"
            else "time-only match (event on a different resource)",
        }
    else:
        f["context"] = {"status": "cause not confirmed: no matching CloudTrail event"}
    return f


def build(report: DayReport) -> dict[str, Any]:
    """Build the fact sheet for a DayReport."""
    basis = {
        "same_weekday": "median of the same weekday over the previous weeks",
        "trailing": "median of the previous 7 days",
        "new": "no earlier data",
        "none": "no baseline available",
    }[report.baseline_method]
    facts: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "day": report.day.strftime("%A %d %B %Y"),
        "total_spend": fmt_usd(report.total_cents),
        "typical_spend": fmt_usd(report.baseline_cents),
        "change": fmt_usd(report.delta_cents, sign=True),
        "change_pct": _pct(report.pct),
        "direction": _direction(report.delta_cents),
        "baseline_basis": basis,
        "significant_change": report.significant,
        "causes": [_cause_facts(c) for c in report.causes],
        "untagged": {
            "amount": fmt_usd(report.untagged.cents),
            "share": _pct(report.untagged.share),
            "resource_count": len(report.untagged.resources),
        },
        "waste": {
            "monthly_total": fmt_usd(sum(w.monthly_savings_cents for w in report.waste)),
            "count": len(report.waste),
            "top": [
                {
                    "resource": w.resource_id,
                    "summary": w.summary,
                    "monthly_saving": fmt_usd(w.monthly_savings_cents),
                    "saving_basis": "from your bill"
                    if w.savings_source == "cur"
                    else "rough estimate",
                    "fix": w.fix,
                }
                for w in report.waste[:3]
            ],
        },
        "warnings": list(report.warnings),
    }
    return facts


def facts_hash(facts: dict[str, Any], prompt_version: str = "") -> str:
    """Stable hash of a fact sheet, used as the cache key for generated text."""
    blob = json.dumps(facts, sort_keys=True, separators=(",", ":")) + prompt_version
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
