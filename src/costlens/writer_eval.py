"""Evaluate a language-model writer against a fixed set of fact sheets.

    python -m costlens.writer_eval --dry-run   # wiring check with a scripted writer, no API call
    python -m costlens.writer_eval --live      # real Claude calls: needs an API key, costs money

For each fact sheet it reports how often the writer's first answer passes the validator, how often
it needs the one retry, and how often the digest falls back to template text, plus tokens used.
It has NOT been run against a real model yet: it needs an API key, and each live run makes one
request per fact sheet (more on retries).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .explain import AnthropicWriter, FakeWriter, TokenBudget, Writer, explain, template_text
from .factsheet import build
from .report import analyze_day
from .synth import INCIDENT_KINDS, generate

NOW = datetime(2026, 12, 1, tzinfo=UTC)


def fact_sheets(per_kind: int = 5) -> list[dict]:
    """30+ fact sheets: every incident kind over several seeds, plus quiet days."""
    sheets = []
    for kind in INCIDENT_KINDS:
        for seed in range(per_kind):
            ds = generate(seed=seed, incident=kind)
            sheets.append(build(analyze_day(ds.rows, ds.last_day, ds.cloudtrail, now=NOW)))
    for seed in range(3):
        ds = generate(seed=100 + seed)
        sheets.append(build(analyze_day(ds.rows, ds.last_day, now=NOW)))
    return sheets


@dataclass(slots=True)
class WriterReport:
    n: int = 0
    first_try: int = 0
    after_retry: int = 0
    fell_back: int = 0
    tokens: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    examples: list[str] = field(default_factory=list)


def evaluate(writer: Writer, sheets: list[dict], budget: TokenBudget | None = None) -> WriterReport:
    rep = WriterReport()
    budget = budget or TokenBudget(limit=10**9)
    for facts in sheets:
        before = budget.used
        exp = explain(facts, writer, budget=budget)
        rep.n += 1
        rep.tokens += budget.used - before
        if exp.source == "llm" and exp.attempts == 1:
            rep.first_try += 1
        elif exp.source == "llm":
            rep.after_retry += 1
        else:
            rep.fell_back += 1
            key = exp.fallback_reason or "unknown"
            rep.reasons[key] = rep.reasons.get(key, 0) + 1
        for problems in exp.rejected:
            if len(rep.examples) < 8:
                rep.examples.append("; ".join(problems[:2]))
    return rep


def render(rep: WriterReport, label: str) -> str:
    def pct(n: int) -> str:
        return f"{n}/{rep.n} ({100 * n / rep.n:.0f}%)" if rep.n else "n/a"

    lines = [
        f"# Writer evaluation: {label}",
        "",
        f"Fact sheets: {rep.n}",
        f"- accepted on the first try: {pct(rep.first_try)}",
        f"- accepted after one retry: {pct(rep.after_retry)}",
        f"- fell back to template text: {pct(rep.fell_back)}",
        f"- tokens used: {rep.tokens}",
    ]
    for reason, count in rep.reasons.items():
        lines.append(f"  - fallback reason: {reason} x{count}")
    if rep.examples:
        lines += ["", "Example rejections:"] + [f"- {e}" for e in rep.examples]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    ap.add_argument("--per-kind", type=int, default=5)
    ap.add_argument("--token-budget", type=int, default=400_000)
    args = ap.parse_args(argv)

    sheets = fact_sheets(args.per_kind)
    if args.live:
        writer: Writer = AnthropicWriter()
        label = f"live ({writer.model})"
        print(f"Making up to {len(sheets) * 2} real API requests...", file=sys.stderr)
    else:
        replies = [json.dumps(template_text(f)) for f in sheets]
        writer = FakeWriter(replies)
        label = "dry run (scripted writer)"
    print(render(evaluate(writer, sheets, TokenBudget(args.token_budget)), label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
