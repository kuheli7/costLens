"""Acknowledge and Snooze must change what the next digest shows, not just a database row."""

import json
from datetime import UTC, datetime, timedelta

from costlens.pipeline import run_day
from costlens.slack import ACK_ACTION, SNOOZE_ACTION, handle_interaction
from costlens.state import State
from costlens.synth import generate

NOW = datetime(2026, 12, 1, tzinfo=UTC)


def has_buttons(blocks):
    return any(b["type"] == "actions" for b in blocks)


def click(action, key, user):
    return {"user": {"username": user}, "actions": [{"action_id": action, "value": key}]}


def run(ds, state=None, now=NOW):
    return run_day(ds.rows, ds.last_day, cloudtrail=ds.cloudtrail, now=now, state=state)


def test_live_alert_shows_buttons_then_acknowledged_alert_says_so_instead():
    ds = generate(seed=7, incident="new_big_instance")
    state = State()
    first = run(ds, state)
    assert first.is_alert and has_buttons(first.blocks) and first.note is None
    handle_interaction(state, click(ACK_ACTION, first.alert_key, "asha"), NOW)
    again = run(ds, state)
    assert not again.is_alert and not has_buttons(again.blocks)
    assert "acknowledged by asha" in again.digest_text
    assert "acknowledged by asha" in json.dumps(again.blocks)


def test_snoozed_alert_names_the_resume_time():
    ds = generate(seed=7, incident="new_big_instance")
    state = State()
    key = run(ds).alert_key
    handle_interaction(state, click(SNOOZE_ACTION, key, "ravi"), NOW)
    res = run(ds, state)
    assert not res.is_alert and "snoozed by ravi until 2026-12-02 00:00 UTC" in res.digest_text


def test_alert_resumes_after_the_snooze_expires():
    ds = generate(seed=7, incident="new_big_instance")
    state = State()
    key = run(ds).alert_key
    handle_interaction(state, click(SNOOZE_ACTION, key, "ravi"), NOW)
    later = run(ds, state, NOW + timedelta(hours=25))
    assert later.is_alert and has_buttons(later.blocks)


def test_acknowledging_one_service_does_not_silence_another():
    ds = generate(seed=7, incident="new_big_instance")
    state = State()
    handle_interaction(state, click(ACK_ACTION, "cost:AmazonRDS", "asha"), NOW)
    assert run(ds, state).is_alert  # the EC2 alert is a different key


def test_quiet_day_offers_no_buttons():
    ds = generate(seed=2)
    assert not has_buttons(run(ds).blocks)
