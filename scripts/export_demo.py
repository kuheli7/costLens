"""Export a static demo dataset for the dashboard (dashboard/public/demo.json).

Runs the real pipeline over 30 days of a synthetic account that has an incident, so the
dashboard can be shown without an AWS account. Everything in the file is synthetic.

    python scripts/export_demo.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from costlens.pipeline import run_day  # noqa: E402
from costlens.state import State  # noqa: E402
from costlens.synth import generate  # noqa: E402
from costlens.waste import FixtureMetrics, Inventory, PriceBook, run_all  # noqa: E402
from costlens.waste.inventory import Address, Volume  # noqa: E402

OUT = ROOT / "dashboard" / "public" / "demo.json"


def main() -> None:
    ds = generate(seed=11, incident="asg_scale_out", incident_age=3)
    state = State()
    now = datetime(2026, 12, 1, tzinfo=UTC)
    created = datetime(2026, 7, 1, tzinfo=UTC)
    inventory = Inventory(
        "ap-south-1",
        volumes=[
            Volume("vol-0a1b2c3d4e5f60718", "available", 500, created, {}),
            Volume("vol-0f9e8d7c6b5a43210", "available", 120, created, {"team": "data"}),
        ],
        addresses=[Address("eipalloc-0123456789abcdef0", "13.233.10.20", False, {})],
    )
    prices = PriceBook(r for r in ds.rows if r.day >= ds.last_day - timedelta(days=7))
    findings, _ = run_all(inventory, FixtureMetrics(), prices, now=now)

    details = {}
    for k in range(29, -1, -1):
        day = ds.last_day - timedelta(days=k)
        res = run_day(ds.rows, day, cloudtrail=ds.cloudtrail, waste=findings, now=now, state=state)
        details[day.isoformat()] = {
            "facts": res.facts,
            "explanation": res.explanation.as_dict(),
            "digest_text": res.digest_text,
        }

    days = state.list_days(30)
    demo = {
        "synthetic": True,
        "days": days,
        "trend": [
            {k: d[k] for k in ("day", "total_cents", "baseline_cents", "significant")}
            for d in reversed(days)
        ],
        "explanations": [
            {
                "day": e["day"],
                "source": e["source"],
                "facts_hash": e["facts_hash"],
                "headline": e["explanation"]["headline"],
                "digest_text": e["digest_text"],
            }
            for e in state.explanations(30)
        ],
        "waste": {"findings": state.findings(), "savings": state.savings_summary()},
        "details": details,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(demo, indent=1), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(days)} days, {len(findings)} waste findings")


if __name__ == "__main__":
    main()
