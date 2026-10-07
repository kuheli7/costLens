import json
from datetime import UTC, datetime, timedelta

import boto3
import duckdb
import pytest
from moto import mock_aws

from costlens.handlers import (
    Config,
    Deps,
    S3StateSync,
    digest,
    ingest,
    list_cur_objects,
    lookup_cloudtrail,
)
from costlens.store import S3ParquetStore
from costlens.synth import generate

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)  # the morning after the last synthetic day
BUCKET = "costlens-test-data"


def cur_parquet(rows, path):
    """Write synthetic rows in CUR 2.0 shape: one hourly-style line per row, tags as a MAP."""
    con = duckdb.connect()
    con.execute(
        """CREATE TABLE cur (
            line_item_usage_start_date TIMESTAMP, line_item_unblended_cost DECIMAL(18,8),
            line_item_usage_amount DOUBLE, line_item_resource_id VARCHAR,
            line_item_product_code VARCHAR, product_region_code VARCHAR,
            line_item_usage_type VARCHAR, line_item_line_item_type VARCHAR,
            tags MAP(VARCHAR, VARCHAR))"""
    )
    for r in rows:
        tags = {}
        if r.team:
            tags["user_team"] = r.team
        if r.asg:
            tags["aws_autoscaling_groupName"] = r.asg
        con.execute(
            "INSERT INTO cur VALUES (?,?,?,?,?,?,?,?,?)",
            [f"{r.day.isoformat()} 00:00:00", f"{r.cost_cents / 100:.2f}", r.usage_qty, r.resource_id,
             r.service, r.region, r.usage_type, r.line_type, tags],
        )  # fmt: skip
    con.execute(f"COPY cur TO '{path}' (FORMAT PARQUET)")


@pytest.fixture()
def world(tmp_path):
    with mock_aws():
        s3 = boto3.client("s3", region_name="ap-south-1")
        s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"}
        )
        ds = generate(seed=7, days=40, incident="new_big_instance")
        f = tmp_path / "cur.parquet"
        month_rows = [r for r in ds.rows if r.day.month == 10]
        cur_parquet(ds.rows, str(f))
        for month in ("2026-09", "2026-10"):
            s3.upload_file(
                str(f), BUCKET, f"cur/export/data/BILLING_PERIOD={month}/part-0001.parquet"
            )
        yield s3, ds, month_rows


class StubCE:
    def __init__(self, ds):
        self.ds = ds

    def get_cost_and_usage(self, **kw):
        start = datetime.fromisoformat(kw["TimePeriod"]["Start"]).date()
        end = datetime.fromisoformat(kw["TimePeriod"]["End"]).date()
        out = []
        d = start
        while d < end:
            total = sum(r.cost_cents for r in self.ds.rows if r.day == d)
            out.append({"TimePeriod": {"Start": d.isoformat(), "End": (d + timedelta(days=1)).isoformat()},
                        "Total": {"UnblendedCost": {"Amount": f"{total / 100:.2f}"}}})  # fmt: skip
            d += timedelta(days=1)
        return {"ResultsByTime": out}


class StubCloudTrail:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def lookup_events(self, **kw):
        self.calls.append(kw)
        if "NextToken" not in kw:
            return {"Events": self.events[:1], "NextToken": "n"}
        return {"Events": self.events[1:]}


def cfg(**kw):
    return Config(bucket=BUCKET, cur_prefix="cur", waste=False, **kw)


def test_ingest_loads_the_target_day_and_the_three_before_it(world):
    s3, ds, _ = world
    deps = Deps(s3=s3, ce=StubCE(ds), now=lambda: NOW)
    out = ingest({}, cfg(), deps)
    assert out["day"] == "2026-10-06" and sorted(out["loaded"]) == [
        "2026-10-03",
        "2026-10-04",
        "2026-10-05",
        "2026-10-06",
    ]
    store = S3ParquetStore(s3, BUCKET, "normalized")
    rows = store.read_range(ds.last_day, ds.last_day)
    assert sum(r.cost_cents for r in rows) == sum(
        r.cost_cents for r in ds.rows if r.day == ds.last_day
    )
    assert out["ce_totals_saved"] == 4


def test_ingest_twice_gives_identical_data(world):
    s3, ds, _ = world
    deps = Deps(s3=s3, ce=StubCE(ds), now=lambda: NOW)
    ingest({}, cfg(), deps)
    store = S3ParquetStore(s3, BUCKET, "normalized")
    first = store.read_range(ds.first_day, ds.last_day)
    ingest({}, cfg(), deps)
    assert store.read_range(ds.first_day, ds.last_day) == first


def test_ingest_for_an_explicit_date(world):
    s3, ds, _ = world
    out = ingest({"date": "2026-10-01"}, cfg(), Deps(s3=s3, now=lambda: NOW))
    assert out["day"] == "2026-10-01" and "2026-10-01" in out["loaded"]


def test_ingest_with_no_cur_files_yet_warns_instead_of_failing():
    with mock_aws():
        s3 = boto3.client("s3", region_name="ap-south-1")
        s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-south-1"}
        )
        out = ingest({}, cfg(), Deps(s3=s3, now=lambda: NOW))
    assert out["loaded"] == {} and "no CUR files" in out["warning"]


def test_list_cur_objects_matches_only_the_billing_month_and_parquet(world):
    s3, ds, _ = world
    s3.put_object(
        Bucket=BUCKET, Key="cur/export/data/BILLING_PERIOD=2026-10/manifest.json", Body=b"{}"
    )
    keys = list_cur_objects(s3, BUCKET, "cur", ds.last_day)
    assert keys == ["cur/export/data/BILLING_PERIOD=2026-10/part-0001.parquet"]


def test_digest_end_to_end_posts_a_slack_message_and_saves_state(world):
    s3, ds, _ = world
    sm = boto3.client("secretsmanager", region_name="ap-south-1")
    sm.create_secret(Name="slack", SecretString=json.dumps({"bot_token": "xoxb-test"}))
    sent = []

    def fake_post(url, data, headers):
        sent.append((url, json.loads(data), headers))
        return {"ok": True}

    deps = Deps(
        s3=s3, ce=StubCE(ds), cloudtrail=StubCloudTrail(ds.cloudtrail), secrets=sm,
        http_post=fake_post, now=lambda: NOW,
    )  # fmt: skip
    c = cfg(slack_channel="C123")
    ingest({}, c, deps)
    out = digest({}, c, deps, slack_secret_id="slack")
    assert out["sent"] and out["significant"] and out["is_alert"] and out["source"] == "template"
    (url, body, headers) = sent[0]
    assert url.endswith("chat.postMessage") and headers["Authorization"] == "Bearer xoxb-test"
    assert body["channel"] == "C123" and body["blocks"][-1]["type"] == "actions"
    assert (
        "alice" in body["text"]
    )  # the CloudTrail actor made it through ingest -> report -> digest
    # state persisted to S3 so the next run (and button clicks) can see it
    assert s3.head_object(Bucket=BUCKET, Key="state/costlens.sqlite")["ContentLength"] > 0


def test_second_digest_run_replaces_the_day_in_the_synced_state(world, tmp_path):
    s3, ds, _ = world
    deps = Deps(s3=s3, ce=StubCE(ds), cloudtrail=StubCloudTrail(ds.cloudtrail), now=lambda: NOW)
    c = cfg()
    ingest({}, c, deps)
    digest({}, c, deps)
    digest({}, c, deps)
    sync = S3StateSync(s3, BUCKET, c.state_key, str(tmp_path / "check.sqlite"))
    state = sync.pull()
    assert len(state.list_days()) == 1 and len(state.explanations()) == 1


def test_digest_without_data_says_so_and_sends_nothing(world):
    s3, ds, _ = world
    out = digest({"date": "2026-10-06"}, cfg(), Deps(s3=s3, now=lambda: NOW))
    assert out["sent"] is False and "no cost data" in out["warning"]


def test_cloudtrail_lookup_follows_pagination():
    ev = [{"EventName": "a"}, {"EventName": "b"}, {"EventName": "c"}]
    stub = StubCloudTrail(ev)
    got = lookup_cloudtrail(stub, NOW - timedelta(days=1), NOW)
    assert [e["EventName"] for e in got] == ["a", "b", "c"] and len(stub.calls) == 2


def test_config_from_environment():
    c = Config.from_env({"COSTLENS_BUCKET": "b", "COSTLENS_AI": "ON", "COSTLENS_WASTE": "off",
                         "COSTLENS_TOKEN_BUDGET": "5000"})  # fmt: skip
    assert (c.bucket, c.ai, c.waste, c.token_budget) == ("b", True, False, 5000)
    assert Config.from_env({"COSTLENS_BUCKET": "b"}).ai is False  # AI is off unless asked for
    with pytest.raises(KeyError):
        Config.from_env({})


def _slack_event(payload, secret, ts, tamper=False, b64=False):
    import base64
    import urllib.parse

    from costlens.slack import sign

    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    sig = sign(secret, str(ts), body)
    if tamper:
        body += b"&x=1"
    return {
        "headers": {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig},
        "body": base64.b64encode(body).decode() if b64 else body.decode(),
        "isBase64Encoded": b64,
    }


def test_slack_button_lambda_updates_the_synced_state_only_for_signed_requests(world, tmp_path):
    from costlens.handlers import interactions
    from costlens.slack import ACK_ACTION

    s3, ds, _ = world
    now = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
    deps = Deps(s3=s3, now=lambda: now)
    c = cfg()
    payload = {
        "user": {"username": "asha"},
        "actions": [{"action_id": ACK_ACTION, "value": "cost:AmazonEC2"}],
    }
    ts = int(now.timestamp())

    bad = interactions(_slack_event(payload, "secret", ts, tamper=True), c, deps, "secret")
    stale = interactions(_slack_event(payload, "secret", ts - 4000), c, deps, "secret")
    assert bad["statusCode"] == 401 and stale["statusCode"] == 401
    sync = S3StateSync(s3, BUCKET, c.state_key, str(tmp_path / "s.sqlite"))
    assert sync.pull().alert("cost:AmazonEC2") is None  # nothing changed

    ok = interactions(_slack_event(payload, "secret", ts, b64=True), c, deps, "secret")
    assert ok["statusCode"] == 200 and "Acknowledged by asha" in json.loads(ok["body"])["text"]
    state = S3StateSync(s3, BUCKET, c.state_key, str(tmp_path / "s2.sqlite")).pull()
    assert state.alert("cost:AmazonEC2")["status"] == "acked"
