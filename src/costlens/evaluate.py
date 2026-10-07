"""Evaluation harness: score the engine against incidents with known causes.

Run it with ``python -m costlens.evaluate``. It injects every incident kind into synthetic
accounts across many seeds, runs the real pipeline, and scores:

* detection        did the day get flagged as a significant change?
* root cause       does the top cause name the injected service / resource / group?
* driver           volume vs rate named correctly (where the label matters)?
* amount           is the cause's dollar amount within tolerance of the true added cost?
* CloudTrail       right actor named when an event exists; no confident guess when none does?
* additivity       do contributions sum to the total change in every dimension?
* faithfulness     does the validator catch deliberately invented facts, and does any invented
                   fact reach a final digest?
* false alarms     how many ordinary days get flagged?

These are SYNTHETIC incidents. They measure the engine against labelled ground truth; they do
not replace testing on a real account, and the README keeps the two apart.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta

from .attribution import (
    baseline_table,
    contributions,
    table_from_rows,
    total_change,
    usage_rows,
)
from .baseline import compute_baseline
from .explain import check_output, explain, template_text
from .factsheet import build
from .models import DIMENSIONS
from .report import DayReport, analyze_day
from .synth import INCIDENT_KINDS, Dataset, generate

NOW = datetime(2026, 12, 1, tzinfo=UTC)  # far enough after the data that nothing is provisional
AMOUNT_TOLERANCE = 0.10  # cause amount within 10% of the true added cost ...
AMOUNT_FLOOR_CENTS = 100  # ... or within a dollar


@dataclass(slots=True)
class IncidentResult:
    kind: str
    seed: int
    age: int
    detected: bool
    cause_correct: bool
    driver_correct: bool | None
    amount_correct: bool
    actor_correct: bool
    additive: bool
    digest_faithful: bool
    tamper_caught: int
    tamper_total: int
    top_scope: dict[str, str] = field(default_factory=dict)
    note: str = ""


def true_added_cents(ds: Dataset, seed: int, age: int, kind: str) -> int:
    """Cost the incident really added on the last day, inside the expected scope."""
    clean = generate(seed=seed)
    inc = ds.incidents[0]

    def scoped(rows):
        return sum(
            r.cost_cents
            for r in rows
            if r.day == ds.last_day
            and r.line_type == "Usage"
            and all(getattr(r, d) == v for d, v in inc.expected.items())
        )

    return scoped(ds.rows) - scoped(clean.rows)


def _additive(report_rows, day) -> bool:
    """Contributions sum to the total change in every dimension (checked on the real inputs)."""
    from .baseline import DEFAULT_CONFIG

    usage = usage_rows(r for r in report_rows if r.day <= day and r.line_type != "Fee")
    by_day: dict[date, list] = {}
    for r in usage:
        by_day.setdefault(r.day, []).append(r)
    series = {d: sum(r.cost_cents for r in rs) for d, rs in by_day.items()}
    base = compute_baseline(series, day, DEFAULT_CONFIG)
    before = baseline_table([table_from_rows(by_day.get(d, [])) for d in (base.comparison_days if base else ())])
    after = table_from_rows(by_day.get(day, []))
    total = total_change(before, after)
    return all(sum(c.delta_cents for c in contributions(before, after, d)) == total for d in DIMENSIONS)


def tampered_variants(facts: dict, rng: random.Random) -> list[str]:
    """Faithful digest JSON with one invented fact inserted, to test the validator."""
    base = template_text(facts)
    out = []
    invented = [
        lambda t: t + " It cost $777.77 extra.",
        lambda t: t + " That is about 412 percent more.",
        lambda t: t + " Resource i-0deadbeefcafe0001 is involved.",
        lambda t: t + " It was changed by mallory.",
        lambda t: t + " It began at 09:41.",
        lambda t: t + " Seventeen more instances were added.",
    ]
    for fn in invented:
        data = json.loads(json.dumps(base))
        target = rng.choice(["headline", "confidence_note", "cause"])
        if target == "cause" and data["causes"]:
            data["causes"][0]["explanation"] = fn(data["causes"][0]["explanation"])
        elif target == "confidence_note":
            data["confidence_note"] = fn(data["confidence_note"])
        else:
            data["headline"] = fn(data["headline"])
        out.append(json.dumps(data))
    return out


def run_incident(kind: str, seed: int, age: int) -> IncidentResult:
    ds = generate(seed=seed, incident=kind, incident_age=age)
    inc = ds.incidents[0]
    rep: DayReport = analyze_day(ds.rows, ds.last_day, ds.cloudtrail, now=NOW)
    facts = build(rep)
    exp = explain(facts)  # template writer: the deterministic floor every digest can fall back to
    detected = rep.significant
    top = rep.causes[0] if rep.causes else None
    cause_ok = bool(top) and all(top.scope.get(d) == v for d, v in inc.expected.items())

    driver_ok: bool | None = None
    if inc.expected_driver and top and top.volume_cents is not None:
        got = "rate" if abs(top.rate_cents) > abs(top.volume_cents) else "volume"
        driver_ok = got == inc.expected_driver

    truth = true_added_cents(ds, seed, age, kind)
    amount_ok = bool(top) and abs(top.delta_cents - truth) <= max(
        AMOUNT_FLOOR_CENTS, AMOUNT_TOLERANCE * abs(truth)
    )

    if top is None:
        actor_ok = False
    elif inc.expected_actor:
        actor_ok = bool(top.match) and top.match.confidence == "exact" and top.match.actor == inc.expected_actor
    else:  # no event exists: the right answer is "not confirmed", never a confident guess
        actor_ok = top.match is None or top.match.confidence == "time_only"

    problems = check_output(json.dumps(template_text(facts)), facts)[1]
    rng = random.Random(seed * 31 + age)
    caught = sum(1 for raw in tampered_variants(facts, rng) if check_output(raw, facts)[0] is None)
    return IncidentResult(
        kind, seed, age, detected, cause_ok, driver_ok, amount_ok, actor_ok,
        _additive(ds.rows, ds.last_day), not problems and exp.source == "template",
        caught, 6, dict(top.scope) if top else {},
        "" if cause_ok else f"expected {inc.expected}",
    )  # fmt: skip


def run_quiet(seeds: range, days_checked: int = 30) -> tuple[int, int]:
    """Count ordinary (incident-free) days flagged as significant."""
    flagged = total = 0
    for seed in seeds:
        ds = generate(seed=seed)
        for k in range(days_checked):
            day = ds.last_day - timedelta(days=k)
            rep = analyze_day(ds.rows, day, now=NOW)
            total += 1
            flagged += rep.significant
    return flagged, total


def pct(n: int, d: int) -> str:
    return "n/a" if d == 0 else f"{n}/{d} ({100 * n / d:.1f}%)"


def summarise(results: list[IncidentResult], quiet: tuple[int, int], seconds: float) -> dict:
    by_kind: dict[str, list[IncidentResult]] = {}
    for r in results:
        by_kind.setdefault(r.kind, []).append(r)
    drivers = [r.driver_correct for r in results if r.driver_correct is not None]
    return {
        "incidents": len(results),
        "detected": sum(r.detected for r in results),
        "cause_correct": sum(r.cause_correct for r in results),
        "driver_correct": (sum(drivers), len(drivers)),
        "amount_correct": sum(r.amount_correct for r in results),
        "actor_correct": sum(r.actor_correct for r in results),
        "additive": sum(r.additive for r in results),
        "digests_faithful": sum(r.digest_faithful for r in results),
        "tamper_caught": (sum(r.tamper_caught for r in results), sum(r.tamper_total for r in results)),
        "quiet_flagged": quiet,
        "seconds": round(seconds, 1),
        "by_kind": {
            k: {
                "n": len(v),
                "cause_correct": sum(r.cause_correct for r in v),
                "detected": sum(r.detected for r in v),
            }
            for k, v in by_kind.items()
        },
        "misses": [asdict(r) for r in results if not (r.cause_correct and r.detected)][:20],
    }


def render_markdown(s: dict, seeds: int, ages: tuple[int, ...]) -> str:
    n = s["incidents"]
    lines = [
        "# CostLens evaluation (synthetic incidents)",
        "",
        f"Generated by `python -m costlens.evaluate`: {len(INCIDENT_KINDS)} incident kinds x {seeds} "
        f"seeds x incident ages {list(ages)} = {n} injected incidents, each on a fresh 90-day "
        "synthetic account.",
        "",
        "These are synthetic. They score the engine against labelled ground truth; they are not a "
        "substitute for testing on a real AWS account.",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Incident detected (day flagged significant) | {pct(s['detected'], n)} |",
        f"| Correct root cause named (top cause matches the injected service/resource/group) | {pct(s['cause_correct'], n)} |",
        f"| Volume vs rate driver named correctly | {pct(*s['driver_correct'])} |",
        f"| Cause amount within {int(AMOUNT_TOLERANCE * 100)}% (or $1) of the true added cost | {pct(s['amount_correct'], n)} |",
        f"| CloudTrail: right actor when an event exists, no confident guess when none does | {pct(s['actor_correct'], n)} |",
        f"| Contributions sum to the total change in every dimension | {pct(s['additive'], n)} |",
        f"| Template digests that pass the fact validator | {pct(s['digests_faithful'], n)} |",
        f"| Deliberately invented facts caught by the validator (6 per digest) | {pct(*s['tamper_caught'])} |",
        f"| Ordinary days wrongly flagged (false alarms) | {pct(*s['quiet_flagged'])} |",
        "",
        "## By incident kind",
        "",
        "| Kind | Runs | Detected | Correct cause |",
        "|---|---|---|---|",
    ]
    for k, v in s["by_kind"].items():
        lines.append(f"| {k} | {v['n']} | {v['detected']}/{v['n']} | {v['cause_correct']}/{v['n']} |")
    lines += ["", "## Misses", ""]
    if not s["misses"]:
        lines.append("None.")
    for m in s["misses"]:
        lines.append(f"- {m['kind']} seed {m['seed']} age {m['age']}: {m['note']}; top scope {m['top_scope']}")
    lines += ["", f"Run time: {s['seconds']} s.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score CostLens against injected cost incidents.")
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--ages", type=int, nargs="+", default=[0, 2])
    ap.add_argument("--quiet-seeds", type=int, default=10)
    ap.add_argument("--out", default="eval/results.md")
    args = ap.parse_args(argv)

    t0 = time.time()
    results = [
        run_incident(kind, seed, age)
        for kind in INCIDENT_KINDS
        for seed in range(args.seeds)
        for age in args.ages
    ]
    quiet = run_quiet(range(1000, 1000 + args.quiet_seeds))
    summary = summarise(results, quiet, time.time() - t0)
    md = render_markdown(summary, args.seeds, tuple(args.ages))
    import os

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.splitext(args.out)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())

