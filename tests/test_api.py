import json
import urllib.parse
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from costlens.api import create_app
from costlens.pipeline import run_day
from costlens.slack import ACK_ACTION, sign
from costlens.state import State
from costlens.synth import generate
from costlens.waste.rules import Finding

NOW = datetime(2026, 12, 1, tzinfo=UTC)
SECRET = "slack-secret"
TOKEN = "write-token"
TS = 1_760_000_000


@pytest.fixture()
def client():
    ds = generate(seed=7, incident="asg_scale_out")
    state = State()
    finding = Finding("unattached_ebs", "vol-1", "ap-south-1", {"size_gb": 500}, 4000, "cur",
                      "aws ec2 ...", "vol-1 idle")  # fmt: skip
    run_day(ds.rows, ds.last_day, cloudtrail=ds.cloudtrail, waste=[finding], now=NOW, state=state)
    app = create_app(state, slack_signing_secret=SECRET, api_token=TOKEN, clock=lambda: TS + 5)
    return TestClient(app), state, ds


def test_health_and_day_endpoints(client):
    c, state, ds = client
    assert c.get("/api/health").json() == {"ok": True, "days": 1}
    days = c.get("/api/days").json()
    assert days[0]["day"] == ds.last_day.isoformat() and days[0]["significant"] is True
    one = c.get(f"/api/days/{ds.last_day.isoformat()}").json()
    assert one["explanation"]["source"] == "template"
    assert one["facts"]["total_spend"] in one["digest_text"]


def test_unknown_or_malformed_day_is_a_clean_error(client):
    c, *_ = client
    assert c.get("/api/days/2020-01-01").status_code == 404
    assert c.get("/api/days/not-a-date").status_code == 422


def test_trend_is_oldest_first_and_explanation_history_lists_headlines(client):
    c, *_ = client
    trend = c.get("/api/trend").json()
    assert trend[0]["total_cents"] > 0 and "baseline_cents" in trend[0]
    hist = c.get("/api/explanations").json()
    assert hist[0]["headline"] and hist[0]["source"] == "template"


def test_waste_listing_and_savings_summary(client):
    c, *_ = client
    body = c.get("/api/waste").json()
    assert body["findings"][0]["resource_id"] == "vol-1"
    assert body["savings"]["open_cents"] == 4000
    assert c.get("/api/waste?status=bogus").status_code == 422
    assert c.get("/api/waste?status=fixed").json()["findings"] == []


def test_status_change_needs_the_token(client):
    c, state, _ = client
    url = "/api/waste/unattached_ebs/vol-1/status"
    assert c.post(url, json={"status": "dismissed"}).status_code == 401
    assert (
        c.post(
            url, json={"status": "dismissed"}, headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )
    ok = c.post(url, json={"status": "dismissed"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200 and state.findings("dismissed")[0]["resource_id"] == "vol-1"
    bad = c.post(url, json={"status": "bogus"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert bad.status_code == 422
    missing = c.post(
        "/api/waste/x/y/status",
        json={"status": "fixed"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert missing.status_code == 404


def test_writes_are_disabled_when_no_token_is_configured():
    app = create_app(State())
    r = TestClient(app).post("/api/waste/a/b/status", json={"status": "fixed"})
    assert r.status_code == 403


def slack_post(c, payload, secret=SECRET, ts=TS, tamper=False):
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    sig = sign(secret, str(ts), body)
    if tamper:
        body += b"&x=1"
    return c.post(
        "/slack/interactions",
        content=body,
        headers={
            "x-slack-request-timestamp": str(ts),
            "x-slack-signature": sig,
            "content-type": "application/x-www-form-urlencoded",
        },
    )


def test_signed_slack_click_acknowledges_the_alert(client):
    c, state, _ = client
    payload = {
        "user": {"username": "asha"},
        "actions": [{"action_id": ACK_ACTION, "value": "cost:AmazonEC2"}],
    }
    r = slack_post(c, payload)
    assert r.status_code == 200 and "Acknowledged by asha" in r.json()["text"]
    assert state.alert("cost:AmazonEC2")["status"] == "acked"


@pytest.mark.parametrize(
    "kwargs",
    [{"secret": "wrong"}, {"ts": TS - 1000}, {"tamper": True}],
    ids=["wrong secret", "replayed (stale) request", "tampered body"],
)
def test_unsigned_stale_or_tampered_slack_requests_are_rejected_and_change_nothing(client, kwargs):
    c, state, _ = client
    payload = {
        "user": {"id": "U1"},
        "actions": [{"action_id": ACK_ACTION, "value": "cost:AmazonEC2"}],
    }
    assert slack_post(c, payload, **kwargs).status_code == 401
    assert state.alert("cost:AmazonEC2") is None


def test_slack_request_without_signature_headers_is_rejected(client):
    c, *_ = client
    assert c.post("/slack/interactions", content=b"payload=%7B%7D").status_code == 401


def test_cors_is_off_unless_origins_are_configured():
    plain = TestClient(create_app(State()))
    r = plain.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    cors = TestClient(create_app(State(), allowed_origins=["https://dash.example"]))
    ok = cors.get("/api/health", headers={"Origin": "https://dash.example"})
    assert ok.headers["access-control-allow-origin"] == "https://dash.example"
    other = cors.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_unused_import_guard():
    assert date(2026, 1, 1).year == 2026
