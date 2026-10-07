"""Turning AI on must never take the daily digest down."""

import json

import boto3

from costlens.handlers import Deps, digest, ingest
from test_handlers import NOW, StubCE, StubCloudTrail, cfg, world  # noqa: F401  (fixtures)


def run_digest(world, llm_secret):  # noqa: F811
    s3, ds, _ = world
    sm = boto3.client("secretsmanager", region_name="ap-south-1")
    sm.create_secret(Name="slack", SecretString=json.dumps({"bot_token": "xoxb-test"}))
    if llm_secret is None:
        sm.create_secret(Name="llm")  # a container with no value yet, as Terraform leaves it
    else:
        sm.create_secret(Name="llm", SecretString=llm_secret)
    posted = []
    deps = Deps(
        s3=s3,
        ce=StubCE(ds),
        cloudtrail=StubCloudTrail(ds.cloudtrail),
        secrets=sm,
        http_post=lambda url, data, headers: posted.append(json.loads(data)) or {"ok": True},
        now=lambda: NOW,
    )
    c = cfg(slack_channel="C1", ai=True)
    ingest({}, c, deps)
    return digest({}, c, deps, slack_secret_id="slack", llm_secret_id="llm"), posted


def test_empty_llm_secret_falls_back_instead_of_failing(world):  # noqa: F811
    out, posted = run_digest(world, None)
    assert out["sent"] and out["source"] == "template"
    assert "AI unavailable" in out["ai_warning"] and len(posted) == 1


def test_malformed_llm_secret_falls_back_too(world):  # noqa: F811
    out, _ = run_digest(world, "not json")
    assert out["sent"] and out["source"] == "template" and out["ai_warning"]


def test_missing_anthropic_sdk_in_the_package_falls_back(world, monkeypatch):  # noqa: F811
    import costlens.handlers as h

    def boom():
        raise ImportError("No module named 'anthropic'")

    monkeypatch.setattr(h, "AnthropicWriter", boom)
    out, _ = run_digest(world, json.dumps({"api_key": "sk-test"}))
    assert out["sent"] and out["source"] == "template" and "ImportError" in out["ai_warning"]


def test_ai_off_reports_no_warning(world):  # noqa: F811
    s3, ds, _ = world
    deps = Deps(s3=s3, ce=StubCE(ds), cloudtrail=StubCloudTrail(ds.cloudtrail), now=lambda: NOW)
    ingest({}, cfg(), deps)
    out = digest({}, cfg(), deps)
    assert out["ai_warning"] is None and out["fallback_reason"] == "AI is off"
