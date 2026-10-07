import json
import random
from datetime import UTC, datetime, timedelta

from costlens.baseline import DEFAULT_CONFIG
from costlens.evaluate import (
    PAIRS,
    TIERS,
    main,
    run_incident,
    run_pair,
    run_quiet,
    tampered_variants,
)
from costlens.explain import FakeWriter, check_output, template_text
from costlens.factsheet import build
from costlens.pipeline import alert_key_for, run_day
from costlens.report import analyze_day
from costlens.slack import ACK_ACTION, handle_interaction
from costlens.state import State
from costlens.synth import INCIDENT_KINDS, generate

NOW = datetime(2026, 12, 1, tzinfo=UTC)


# ----------------------------------------------------------------- pipeline


def test_run_day_produces_digest_blocks_and_stores_everything():
    ds = generate(seed=7, incident="new_big_instance")
    state = State()
    res = run_day(ds.rows, ds.last_day, cloudtrail=ds.cloudtrail, now=NOW, state=state)
    assert res.report.significant and res.is_alert
    assert res.alert_key == "cost:AmazonEC2"
    assert res.facts["total_spend"] in res.digest_text
    assert res.blocks[-1]["type"] == "actions"
    stored = state.explanation_for(ds.last_day)
    assert stored["digest_text"] == res.digest_text
    assert stored["facts"] == json.loads(json.dumps(res.facts))


def test_acknowledged_alert_is_not_resent_but_the_digest_still_is():
    ds = generate(seed=7, incident="new_big_instance")
    state = State()
    first = run_day(ds.rows, ds.last_day, cloudtrail=ds.cloudtrail, now=NOW, state=state)
    handle_interaction(
        state, {"user": {"id": "U1"}, "actions": [{"action_id": ACK_ACTION, "value": first.alert_key}]}, NOW
    )
    again = run_day(
        ds.rows, ds.last_day, cloudtrail=ds.cloudtrail, now=NOW + timedelta(hours=2), state=state
    )
    assert again.report.significant and not again.is_alert
    assert again.digest_text  # the daily digest is still produced


def test_rerunning_a_day_replaces_its_stored_record():
    ds = generate(seed=7, incident="nat_traffic")
    state = State()
    for _ in range(3):
        run_day(ds.rows, ds.last_day, cloudtrail=ds.cloudtrail, now=NOW, state=state)
    assert len(state.list_days()) == 1 and len(state.explanations()) == 1


def test_quiet_day_digest_says_no_significant_change_and_does_not_alert():
    ds = generate(seed=2)
    res = run_day(ds.rows, ds.last_day, now=NOW)
    assert not res.is_alert and "in line with usual" in res.digest_text
    assert alert_key_for(res.report) == "cost:total"


def test_writer_output_with_an_invented_number_never_reaches_the_digest():
    ds = generate(seed=7, incident="new_big_instance")
    facts = build(analyze_day(ds.rows, ds.last_day, ds.cloudtrail, now=NOW))
    bad = template_text(facts)
    bad["headline"] += " That is $4,242.42 more than last year."
    writer = FakeWriter([json.dumps(bad), json.dumps(bad)])
    res = run_day(ds.rows, ds.last_day, cloudtrail=ds.cloudtrail, now=NOW, writer=writer)
    assert res.explanation.source == "template" and "4,242.42" not in res.digest_text


# ----------------------------------------------------------------- evaluation harness


def test_every_tampered_variant_is_rejected_for_every_incident_kind():
    for kind in INCIDENT_KINDS:
        ds = generate(seed=3, incident=kind)
        facts = build(analyze_day(ds.rows, ds.last_day, ds.cloudtrail, now=NOW))
        variants = tampered_variants(facts, random.Random(1))
        assert len(variants) == 6
        assert all(check_output(v, facts)[0] is None for v in variants), kind


def test_single_incident_run_scores_all_fields():
    r = run_incident("new_big_instance", 1, 0, TIERS[0])
    assert (r.detected, r.cause_correct, r.amount_correct, r.actor_correct, r.additive) == (
        True, True, True, True, True,
    )  # fmt: skip
    assert r.tamper_caught == r.tamper_total == 6 and r.digest_faithful


def test_rate_incident_is_scored_on_the_driver():
    r = run_incident("rate_change", 1, 0, TIERS[0])
    assert r.driver_correct is True and r.cause_correct


def test_incidents_under_the_alert_floor_are_marked_not_alertable():
    tiny = TIERS[0].__class__("tiny", 0.03, 0.1, (0,))
    r = run_incident("new_service", 1, 0, tiny)
    assert not r.alertable and not r.detected  # silent by design


def test_pair_run_finds_both_incidents():
    r = run_pair(PAIRS[0], 1, TIERS[0])
    assert r.both_in_top3 and r.larger_first


def test_no_false_alarms_on_ordinary_days():
    flagged, total = run_quiet(range(500, 502), 0.03, days_checked=20)
    assert total == 40 and flagged == 0


def test_evaluation_cli_writes_markdown_and_json(tmp_path):
    out = tmp_path / "results.md"
    assert main(["--seeds", "1", "--quiet-seeds", "1", "--out", str(out)]) == 0
    md = out.read_text(encoding="utf-8")
    assert "Tier: standard" in md and "Tier: hard" in md and "False alarms" in md
    data = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert data["tiers"]["standard"]["incidents"] == len(INCIDENT_KINDS) * 2
    assert data["tiers"]["standard"]["additive"] == data["tiers"]["standard"]["incidents"]
    assert DEFAULT_CONFIG.min_abs_cents == 500
