import json
import urllib.parse
from datetime import UTC, date, datetime, timedelta

import pytest

from costlens.explain import explain
from costlens.factsheet import build
from costlens.report import analyze_day
from costlens.slack import (
    ACK_ACTION,
    SNOOZE_ACTION,
    build_blocks,
    escape,
    handle_interaction,
    parse_interaction_body,
    post_message,
    render_digest,
    sign,
    verify_signature,
)
from costlens.state import State
from costlens.synth import generate
from costlens.waste.rules import Finding

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def make(kind="asg_scale_out", waste=()):
    ds = generate(seed=7, incident=kind)
    rep = analyze_day(ds.rows, ds.last_day, ds.cloudtrail, waste, now=NOW)
    facts = build(rep)
    return rep, facts, explain(facts)


def finding(rid="vol-1", cents=4000, source="cur", rule="unattached_ebs"):
    return Finding(
        rule, rid, "ap-south-1", {"size_gb": 500}, cents, source, "aws ec2 ...", f"{rid} idle"
    )


# ----------------------------------------------------------------- digest and blocks


def test_digest_text_has_numbers_from_the_facts_and_a_fix_per_cause():
    rep, facts, exp = make()
    text = render_digest(facts, exp)
    assert facts["total_spend"] in text and facts["change"] in text
    assert text.count("Fix:") == len(exp.causes) == 1
    assert "deploy-bot" in text


def test_blocks_are_valid_block_kit_with_both_buttons():
    rep, facts, exp = make()
    blocks = build_blocks(facts, exp, "cost:total", "https://example.test/d")
    assert blocks[0]["type"] == "header" and len(blocks[0]["text"]["text"]) <= 150
    actions = blocks[-1]
    assert actions["type"] == "actions"
    ids = {e["action_id"] for e in actions["elements"]}
    assert {ACK_ACTION, SNOOZE_ACTION, "costlens_details"} == ids
    assert all(e["value"] == "cost:total" for e in actions["elements"] if "value" in e)
    for b in blocks:
        if b["type"] == "section":
            assert len(b["text"]["text"]) <= 3000
    json.dumps(blocks)  # serialisable


def test_blocks_include_waste_and_untagged_lines():
    rep, facts, exp = make(waste=[finding()])
    text = json.dumps(build_blocks(facts, exp, "k"))
    assert "Waste found" in text and "Untagged spend" in text


def test_slack_control_characters_are_escaped():
    assert escape("a < b & c > d") == "a &lt; b &amp; c &gt; d"


# ----------------------------------------------------------------- signatures


SECRET = "8f742231b10e8888abcd99yyyzzz85a5"


def test_valid_signature_passes():
    body, ts = b"payload=%7B%7D", "1760000000"
    assert verify_signature(SECRET, ts, body, sign(SECRET, ts, body), now=1760000010)


@pytest.mark.parametrize(
    "secret, ts, body, sig_secret, now",
    [
        (SECRET, "1760000000", b"tampered", SECRET, 1760000010),  # body changed after signing
        (SECRET, "1760000000", b"x", "other-secret", 1760000010),  # wrong signing secret
        (SECRET, "1760000000", b"x", SECRET, 1760000000 + 301),  # replay: too old
        (SECRET, "1760000000", b"x", SECRET, 1760000000 - 301),  # too far in the future
    ],
)
def test_bad_signatures_fail(secret, ts, body, sig_secret, now):
    signed = sign(sig_secret, ts, b"x" if body == b"tampered" else body)
    assert not verify_signature(secret, ts, body, signed, now=now)


def test_missing_or_malformed_parts_fail_closed():
    assert not verify_signature(SECRET, None, b"x", "v0=abc", now=1)
    assert not verify_signature(SECRET, "1", b"x", None, now=1)
    assert not verify_signature(SECRET, "not-a-number", b"x", "v0=abc", now=1)
    assert not verify_signature("", "1", b"x", "v0=abc", now=1)


# ----------------------------------------------------------------- buttons


def click(action_id, key="cost:total", user="asha"):
    return {
        "user": {"username": user, "id": "U1"},
        "actions": [{"action_id": action_id, "value": key}],
    }


def test_interaction_body_round_trip():
    payload = click(ACK_ACTION)
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    assert parse_interaction_body(body) == payload


def test_acknowledge_silences_the_alert_for_the_rest_of_the_day_only():
    s = State()
    resp = handle_interaction(s, click(ACK_ACTION), NOW)
    assert "Acknowledged by asha" in resp["text"]
    assert not s.should_alert("cost:total", NOW + timedelta(hours=5))
    assert s.should_alert("cost:total", NOW + timedelta(days=1))


def test_snooze_silences_for_24_hours_then_resumes():
    s = State()
    handle_interaction(s, click(SNOOZE_ACTION), NOW)
    assert not s.should_alert("cost:total", NOW + timedelta(hours=23))
    assert s.should_alert("cost:total", NOW + timedelta(hours=25))


def test_unknown_alert_key_alerts_and_unknown_action_is_ignored():
    s = State()
    assert s.should_alert("cost:never-seen", NOW)
    assert "Unknown" in handle_interaction(s, click("nope"), NOW)["text"]
    assert handle_interaction(s, {"actions": []}, NOW)["text"] == "Nothing to do."


# ----------------------------------------------------------------- sending


def test_post_message_sends_a_bearer_request_and_raises_on_slack_errors():
    sent = {}

    def ok(url, data, headers):
        sent.update(url=url, data=json.loads(data), headers=headers)
        return {"ok": True, "ts": "1.2"}

    assert post_message("xoxb-t", "C1", [{"type": "divider"}], "fallback", ok)["ok"]
    assert (
        sent["url"].endswith("chat.postMessage")
        and sent["headers"]["Authorization"] == "Bearer xoxb-t"
    )
    assert sent["data"]["channel"] == "C1" and sent["data"]["text"] == "fallback"
    with pytest.raises(RuntimeError, match="channel_not_found"):
        post_message("t", "C1", [], "x", lambda *a: {"ok": False, "error": "channel_not_found"})


# ----------------------------------------------------------------- state store


def test_day_and_explanation_are_stored_together_and_replace_on_rerun():
    s = State()
    rep, facts, exp = make()
    digest = render_digest(facts, exp)
    s.save_day(rep, facts, exp, digest, NOW)
    s.save_day(rep, facts, exp, digest, NOW + timedelta(hours=1))  # re-run: replaces, no duplicate
    assert len(s.list_days()) == 1 and len(s.explanations()) == 1
    stored = s.explanation_for(rep.day)
    assert stored["facts"]["total_spend"] == facts["total_spend"]
    assert stored["digest_text"] == digest and stored["source"] == "template"


def test_finding_lifecycle_open_then_fixed_then_reopened():
    s = State()
    d1, d2, d3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
    s.record_findings([finding("vol-1"), finding("vol-2", 1000, "estimate")], d1)
    assert {f["resource_id"] for f in s.findings("open")} == {"vol-1", "vol-2"}
    s.record_findings([finding("vol-2", 1000, "estimate")], d2)  # vol-1 disappeared
    fixed = s.findings("fixed")
    assert [f["resource_id"] for f in fixed] == ["vol-1"] and fixed[0][
        "resolved_on"
    ] == "2026-10-02"
    s.record_findings([finding("vol-1")], d3)  # came back
    assert "vol-1" in {f["resource_id"] for f in s.findings("open")}


def test_savings_only_count_cur_priced_fixes_as_saved():
    s = State()
    s.record_findings([finding("a", 4000, "cur"), finding("b", 900, "estimate")], date(2026, 10, 1))
    s.record_findings([], date(2026, 10, 2))  # both fixed
    sm = s.savings_summary()
    assert (
        sm["saved_cents"] == 4000 and sm["saved_estimated_cents"] == 900 and sm["open_cents"] == 0
    )


def test_dismissed_finding_stays_dismissed_when_seen_again():
    s = State()
    s.record_findings([finding("a")], date(2026, 10, 1))
    assert s.set_finding_status("unattached_ebs", "a", "dismissed")
    s.record_findings([finding("a")], date(2026, 10, 2))
    assert s.findings("dismissed")[0]["resource_id"] == "a"
    assert s.savings_summary()["dismissed_cents"] == 4000
    assert not s.set_finding_status("unattached_ebs", "nope", "dismissed")
    with pytest.raises(ValueError):
        s.set_finding_status("unattached_ebs", "a", "bogus")
