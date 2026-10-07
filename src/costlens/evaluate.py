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
import os
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
from .baseline import DEFAULT_CONFIG, compute_baseline
from .explain import check_output, explain, template_text
from .factsheet import build
from .models import DIMENSIONS
from .report import DayReport, analyze_day
from .synth import INCIDENT_KINDS, Dataset, generate

NOW = datetime(2026, 12, 1, tzinfo=UTC)  # far enough after the data that nothing is provisional
AMOUNT_TOLERANCE = 0.10  # cause amount within 10% of the true added cost ...
AMOUNT_FLOOR_CENTS = 100  # ... or within a dollar


@dataclass(frozen=True, slots=True)
class Tier:
    name: str
    noise: float
    scale: float
    ages: tuple[int, ...]


# standard: the clean case. hard: 2.7x the day-to-day noise and incidents at half the size.
TIERS = (Tier("standard", 0.03, 1.0, (0, 2)), Tier("hard", 0.08, 0.5, (0, 2)))

# pairs of incidents injected into the same day; scopes are disjoint so each has one right answer
PAIRS = (
    ("new_big_instance", "rate_change"),
    ("nat_traffic", "new_service"),
    ("asg_scale_out", "new_service"),
)


@dataclass(slots=True)
class IncidentResult:
    kind: str
    seed: int
    age: int
    tier: str
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
    alertable: bool = True  # true added cost reaches the alert floor ($5/day by default)


def true_added_cents(ds: Dataset, seed: int, noise: float, index: int = 0) -> int:
    """Cost the incident really added on the last day, inside the expected scope."""
    clean = generate(seed=seed, noise=noise)
    inc = ds.incidents[index]

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
    usage = usage_rows(r for r in report_rows if r.day <= day and r.line_type != "Fee")
    by_day: dict[date, list] = {}
    for r in usage:
        by_day.setdefault(r.day, []).append(r)
    series = {d: sum(r.cost_cents for r in rs) for d, rs in by_day.items()}
    base = compute_baseline(series, day, DEFAULT_CONFIG)
    before = baseline_table(
        [table_from_rows(by_day.get(d, [])) for d in (base.comparison_days if base else ())]
    )
    after = table_from_rows(by_day.get(day, []))
    total = total_change(before, after)
    return all(
        sum(c.delta_cents for c in contributions(before, after, d)) == total for d in DIMENSIONS
    )


def _unused_number_word(facts: dict) -> str:
    """A number word whose value is not in the fact sheet, so using it is a real invention."""
    from decimal import Decimal

    from .validate import NUMBER_WORDS, Allowed

    present = {Decimal(str(v)) for v in Allowed(facts).numbers}
    for word in ("seventeen", "nineteen", "thirteen", "fourteen", "eighteen"):
        if Decimal(NUMBER_WORDS[word]) not in present:
            return word.capitalize()
    return "Ninety"


def tampered_variants(facts: dict, rng: random.Random) -> list[str]:
    """Faithful digest JSON with one invented fact inserted, to test the validator.

    The invented values are chosen so none of them appears in the fact sheet.
    """
    from .validate import Allowed

    allowed = Allowed(facts)
    amount = next(a for a in ("777.77", "913.21", "654.32") if allowed.number_ok(a) is False)
    percent = next(p for p in ("412", "587", "733") if allowed.number_ok(p) is False)
    clock = next(t for t in ("09:41", "17:58", "21:13") if t not in allowed.times)
    base = template_text(facts)
    word = _unused_number_word(facts)
    invented = [
        lambda t: t + f" It cost ${amount} extra.",
        lambda t: t + f" That is about {percent} percent more.",
        lambda t: t + " Resource i-0deadbeefcafe0001 is involved.",
        lambda t: t + " It was changed by mallory.",
        lambda t: t + f" It began at {clock}.",
        lambda t: t + f" {word} more instances were added.",
    ]
    out = []
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


def run_incident(kind: str, seed: int, age: int, tier: Tier = TIERS[0]) -> IncidentResult:
    ds = generate(seed=seed, incident=kind, incident_age=age, noise=tier.noise, scale=tier.scale)
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

    truth = true_added_cents(ds, seed, tier.noise)
    amount_ok = bool(top) and abs(top.delta_cents - truth) <= max(
        AMOUNT_FLOOR_CENTS, AMOUNT_TOLERANCE * abs(truth)
    )

    if top is None:
        actor_ok = False
    elif inc.expected_actor:
        actor_ok = (
            bool(top.match)
            and top.match.confidence == "exact"
            and top.match.actor == inc.expected_actor
        )
    else:  # no event exists: the right answer is "not confirmed", never a confident guess
        actor_ok = top.match is None or top.match.confidence == "time_only"

    problems = check_output(json.dumps(template_text(facts)), facts)[1]
    rng = random.Random(seed * 31 + age)
    caught = sum(1 for raw in tampered_variants(facts, rng) if check_output(raw, facts)[0] is None)
    return IncidentResult(
        kind, seed, age, tier.name, detected, cause_ok, driver_ok, amount_ok, actor_ok,
        _additive(ds.rows, ds.last_day), not problems and exp.source == "template",
        caught, 6, dict(top.scope) if top else {},
        "" if cause_ok else f"expected {inc.expected}",
        truth >= DEFAULT_CONFIG.min_abs_cents,
    )  # fmt: skip


def run_quiet(seeds: range, noise: float = 0.03, days_checked: int = 30) -> tuple[int, int]:
    """Count ordinary (incident-free) days flagged as significant."""
    flagged = total = 0
    for seed in seeds:
        ds = generate(seed=seed, noise=noise)
        for k in range(days_checked):
            day = ds.last_day - timedelta(days=k)
            rep = analyze_day(ds.rows, day, now=NOW)
            total += 1
            flagged += rep.significant
    return flagged, total


@dataclass(slots=True)
class PairResult:
    kinds: tuple[str, str]
    seed: int
    tier: str
    both_in_top3: bool
    larger_first: bool


def run_pair(kinds: tuple[str, str], seed: int, tier: Tier) -> PairResult:
    """Two incidents on the same day: both must appear among the top three causes, and the
    larger one must rank first."""
    ds = generate(seed=seed, incident=kinds, noise=tier.noise, scale=tier.scale)
    rep = analyze_day(ds.rows, ds.last_day, ds.cloudtrail, now=NOW)
    truths = [true_added_cents(ds, seed, tier.noise, i) for i in range(2)]

    def rank_of(i: int) -> int | None:
        exp = ds.incidents[i].expected
        for n, c in enumerate(rep.causes):
            if all(c.scope.get(d) == v for d, v in exp.items()):
                return n
        return None

    ranks = [rank_of(0), rank_of(1)]
    both = all(r is not None for r in ranks)
    larger = 0 if truths[0] >= truths[1] else 1
    return PairResult(kinds, seed, tier.name, both, both and ranks[larger] == 0)


def pct(n: int, d: int) -> str:
    return "n/a" if d == 0 else f"{n}/{d} ({100 * n / d:.1f}%)"


def summarise_tier(all_results: list[IncidentResult]) -> dict:
    """Score only incidents that reach the alert floor; smaller ones are silent by design."""
    results = [r for r in all_results if r.alertable]
    below_floor = len(all_results) - len(results)
    by_kind: dict[str, list[IncidentResult]] = {}
    for r in results:
        by_kind.setdefault(r.kind, []).append(r)
    drivers = [r.driver_correct for r in results if r.driver_correct is not None]
    return {
        "incidents": len(results),
        "below_floor": below_floor,
        "detected": sum(r.detected for r in results),
        "cause_correct": sum(r.cause_correct for r in results),
        "driver_correct": (sum(drivers), len(drivers)),
        "amount_correct": sum(r.amount_correct for r in results),
        "actor_correct": sum(r.actor_correct for r in results),
        "additive": sum(r.additive for r in results),
        "digests_faithful": sum(r.digest_faithful for r in results),
        "tamper_caught": (
            sum(r.tamper_caught for r in results),
            sum(r.tamper_total for r in results),
        ),
        "by_kind": {
            k: {
                "n": len(v),
                "cause_correct": sum(r.cause_correct for r in v),
                "detected": sum(r.detected for r in v),
            }
            for k, v in by_kind.items()
        },
        "misses": [asdict(r) for r in results if not (r.cause_correct and r.detected)],
    }


def render_markdown(report: dict, seeds: int) -> str:
    lines = [
        "# CostLens evaluation (synthetic incidents)",
        "",
        "Generated by `python -m costlens.evaluate`. Each run injects labelled incidents into a "
        f"fresh 90-day synthetic account ({len(INCIDENT_KINDS)} incident kinds x {seeds} seeds x "
        "incident ages 0 and 2 days per tier), runs the real pipeline, and scores the result "
        "against the label.",
        "",
        "These are synthetic. They score the engine against ground truth injected by the author; "
        "they are not a substitute for testing on a real AWS account.",
        "",
    ]
    for name, t in report["tiers"].items():
        n = t["incidents"]
        cfg = report["tier_config"][name]
        tol = int(AMOUNT_TOLERANCE * 100)
        lines += [
            f"## Tier: {name}",
            "",
            f"Noise {cfg['noise']:.0%} day-to-day, incident size x{cfg['scale']}, "
            f"{n} injected incidents scored; {t['below_floor']} more were smaller than the "
            f"${DEFAULT_CONFIG.min_abs_cents // 100}/day alert floor and are silent by design, "
            "so they are not scored.",
            "",
            "| Metric | Result |",
            "|---|---|",
            f"| Incident detected (day flagged significant) | {pct(t['detected'], n)} |",
            "| Correct root cause named (top cause matches the injected "
            f"service/resource/group) | {pct(t['cause_correct'], n)} |",
            f"| Volume vs rate driver named correctly | {pct(*t['driver_correct'])} |",
            f"| Cause amount within {tol}% (or $1) of the true added cost | "
            f"{pct(t['amount_correct'], n)} |",
            "| CloudTrail: right actor when an event exists, no confident guess when none "
            f"does | {pct(t['actor_correct'], n)} |",
            f"| Contributions sum to the total change in every dimension | "
            f"{pct(t['additive'], n)} |",
            f"| Template digests that pass the fact validator | {pct(t['digests_faithful'], n)} |",
            "| Deliberately invented facts caught by the validator (6 per digest) | "
            f"{pct(*t['tamper_caught'])} |",
            "",
            "| Kind | Runs | Detected | Correct cause |",
            "|---|---|---|---|",
        ]
        for k, v in t["by_kind"].items():
            lines.append(
                f"| {k} | {v['n']} | {v['detected']}/{v['n']} | {v['cause_correct']}/{v['n']} |"
            )
        lines.append("")
    lines += [
        "## Two incidents on the same day",
        "",
        "| Tier | Both causes in the top three | Larger cause ranked first |",
        "|---|---|---|",
    ]
    for name, v in report["pairs"].items():
        lines.append(f"| {name} | {pct(v['both'], v['n'])} | {pct(v['larger_first'], v['n'])} |")
    lines += ["", "## False alarms", "", "| Noise | Ordinary days wrongly flagged |", "|---|---|"]
    for noise, (flagged, total) in report["quiet"].items():
        lines.append(f"| {float(noise):.0%} | {pct(flagged, total)} |")
    lines += ["", "## Misses", ""]
    misses = [m for t in report["tiers"].values() for m in t["misses"]]
    if not misses:
        lines.append("None.")
    for m in misses[:40]:
        lines.append(
            f"- [{m['tier']}] {m['kind']} seed {m['seed']} age {m['age']}: {m['note']}; "
            f"detected={m['detected']}, top scope {m['top_scope']}"
        )
    lines += ["", f"Run time: {report['seconds']} s.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score CostLens against injected cost incidents.")
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--quiet-seeds", type=int, default=10)
    ap.add_argument("--out", default="eval/results.md")
    args = ap.parse_args(argv)

    t0 = time.time()
    tiers: dict[str, dict] = {}
    pairs: dict[str, dict] = {}
    quiet: dict[str, tuple[int, int]] = {}
    for tier in TIERS:
        results = [
            run_incident(kind, seed, age, tier)
            for kind in INCIDENT_KINDS
            for seed in range(args.seeds)
            for age in tier.ages
        ]
        tiers[tier.name] = summarise_tier(results)
        pr = [run_pair(k, seed, tier) for k in PAIRS for seed in range(args.seeds)]
        pairs[tier.name] = {
            "n": len(pr),
            "both": sum(r.both_in_top3 for r in pr),
            "larger_first": sum(r.larger_first for r in pr),
        }
        quiet[str(tier.noise)] = run_quiet(range(1000, 1000 + args.quiet_seeds), tier.noise)
    report = {
        "tiers": tiers,
        "tier_config": {t.name: {"noise": t.noise, "scale": t.scale} for t in TIERS},
        "pairs": pairs,
        "quiet": quiet,
        "seconds": round(time.time() - t0, 1),
    }
    md = render_markdown(report, args.seeds)
    out_dir = os.path.dirname(args.out) or "."
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.splitext(args.out)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
