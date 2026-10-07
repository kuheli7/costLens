"""Policy for days CUR may still revise: yesterday is incomplete when the digest runs."""

from dataclasses import replace
from datetime import UTC, datetime

from costlens.report import analyze_day
from costlens.synth import generate

LATE = datetime(2026, 10, 20, tzinfo=UTC)  # long after the day, so it is final
MORNING_AFTER = datetime(2026, 10, 7, 3, 30, tzinfo=UTC)  # when the daily digest actually runs


def scaled_last_day(ds, factor):
    rows = []
    for r in ds.rows:
        if r.day == ds.last_day:
            r = replace(r, cost_cents=round(r.cost_cents * factor), usage_qty=r.usage_qty * factor)
        rows.append(r)
    return rows


def test_a_partly_delivered_day_is_not_reported_as_a_saving():
    ds = generate(seed=2)  # an ordinary account, no incident
    partial = scaled_last_day(ds, 0.6)  # 60% of yesterday's hours have arrived
    early = analyze_day(partial, ds.last_day, now=MORNING_AFTER)
    assert not early.significant and early.causes == []
    assert any("incomplete" in w for w in early.warnings)


def test_without_the_policy_the_same_partial_day_would_be_a_false_alarm():
    """Same numbers, but the day is final: now a 40% drop is real and must be flagged."""
    ds = generate(seed=2)
    final = analyze_day(scaled_last_day(ds, 0.6), ds.last_day, now=LATE)
    assert final.significant and final.delta_cents < 0


def test_a_real_increase_on_a_provisional_day_is_still_reported_with_a_caveat():
    ds = generate(seed=2, incident="new_big_instance")
    rep = analyze_day(ds.rows, ds.last_day, ds.cloudtrail, now=MORNING_AFTER)
    assert rep.significant and rep.causes
    assert any("provisional" in w for w in rep.warnings)


def test_an_ordinary_provisional_day_carries_no_warning_noise():
    ds = generate(seed=2)
    rep = analyze_day(ds.rows, ds.last_day, now=MORNING_AFTER)
    assert not rep.significant and rep.warnings == []
