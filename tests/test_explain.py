import json
from datetime import UTC, datetime
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from costlens.explain import (
    AnthropicWriter,
    FakeWriter,
    TemplateWriter,
    TokenBudget,
    WriterError,
    check_output,
    explain,
    template_text,
)
from costlens.factsheet import build, facts_hash
from costlens.report import analyze_day
from costlens.synth import INCIDENT_KINDS, generate
from costlens.validate import Allowed, validate_text
from costlens.waste import Inventory, run_all
from costlens.waste.inventory import Address, Volume

LATE = datetime(2026, 10, 20, tzinfo=UTC)


def facts_for(kind="asg_scale_out", seed=7, with_waste=False):
    ds = generate(seed=seed, incident=kind)
    waste = []
    if with_waste:
        inv = Inventory(
            "ap-south-1",
            volumes=[
                Volume(
                    "vol-0123456789abcdef0", "available", 500, datetime(2026, 1, 1, tzinfo=UTC), {}
                )
            ],
            addresses=[Address("eipalloc-0abc123", "13.233.1.2", False, {})],
        )
        waste, _ = run_all(inv, FakeWriterMetrics(), now=LATE)
    rep = analyze_day(ds.rows, ds.last_day, ds.cloudtrail, waste, now=LATE)
    return build(rep)


class FakeWriterMetrics:
    def series(self, *a, **k):
        return []


def reply(facts, **overrides):
    data = template_text(facts)
    data.update(overrides)
    return json.dumps(data)


# ----------------------------------------------------------------- the validator


def test_every_template_digest_passes_the_validator():
    for kind in INCIDENT_KINDS:
        facts = facts_for(kind, with_waste=True)
        data, problems = check_output(json.dumps(template_text(facts)), facts)
        assert problems == [] and data is not None, (kind, problems)


def test_quiet_day_template_passes_too():
    ds = generate(seed=1)
    facts = build(analyze_day(ds.rows, ds.last_day, now=LATE))
    assert facts["causes"] == []
    assert check_output(json.dumps(template_text(facts)), facts)[1] == []


def test_invented_number_is_rejected():
    facts = facts_for("asg_scale_out")
    allowed = Allowed(facts)
    assert validate_text("Set the cooldown to 300 seconds.", allowed) == [
        "number not in facts: 300"
    ]
    assert validate_text("It added $13.50 a day.", allowed) == ["number not in facts: 13.50"]


def test_numbers_from_the_fact_sheet_pass_and_rounding_is_allowed():
    allowed = Allowed({"change": "+$12.00", "pct": "37.4%"})
    assert validate_text("Up $12.00, or $12.", allowed) == []
    assert validate_text("About 37% more.", allowed) == []
    assert validate_text("About 38% more.", allowed) == ["number not in facts: 38"]
    assert validate_text("Up $12.1.", allowed) != []


def test_invented_resource_id_is_rejected_and_real_one_passes():
    facts = facts_for("new_big_instance")
    real = facts["causes"][0]["what"].split("resource ")[1].split(",")[0]
    allowed = Allowed(facts)
    assert validate_text(f"Instance {real} is the cause.", allowed) == []
    assert validate_text("Instance i-0abc999def is the cause.", allowed) == [
        "identifier not in facts: i-0abc999def"
    ]


def test_invented_person_after_by_is_rejected():
    facts = facts_for("asg_scale_out")
    allowed = Allowed(facts)
    assert validate_text("It was changed by deploy-bot.", allowed) == []
    problems = validate_text("It was changed by mallory.", allowed)
    assert problems and "mallory" in problems[0]
    assert validate_text("Spend rose by the new group.", allowed) == []  # ordinary use of 'by'


def test_number_words_are_checked_too():
    allowed = Allowed({"x": "5 instances"})
    assert validate_text("Five instances.", allowed) == []
    assert validate_text("It doubled.", allowed) == ["number word not in facts: doubled"]


def test_times_and_dates_must_come_from_the_facts():
    facts = facts_for("asg_scale_out")
    allowed = Allowed(facts)
    assert validate_text("It happened at 02:14.", allowed) == []
    assert validate_text("It happened at 09:45.", allowed) == ["time not in facts: 09:45"]
    assert validate_text("Started on 2026-10-06.", allowed) == []
    assert validate_text("Started on 2026-09-01.", allowed) == ["date not in facts: 2026-09-01"]


def test_common_hyphenated_words_are_not_flagged_but_unknown_identifiers_are():
    allowed = Allowed({"x": "api-prod-asg"})
    assert validate_text("It did not scale-in; the api-prod-asg group stayed big.", allowed) == []
    assert validate_text("The api-staging-asg group grew.", allowed) == [
        "identifier not in facts: api-staging-asg"
    ]


# ----------------------------------------------------------------- the pipeline


def test_faithful_llm_output_is_accepted():
    facts = facts_for()
    exp = explain(facts, FakeWriter([reply(facts, headline="Spend went up; one group drove it.")]))
    assert exp.source == "llm" and exp.attempts == 1 and exp.rejected == []


def test_invented_number_triggers_one_retry_with_feedback_then_passes():
    facts = facts_for()
    bad = reply(facts, headline="Spend rose by $999.99 today.")
    good = reply(facts, headline="Spend rose today.")
    writer = FakeWriter([bad, good])
    exp = explain(facts, writer)
    assert exp.source == "llm" and exp.attempts == 2
    assert exp.rejected and "999.99" in exp.rejected[0][0]
    assert writer.calls[0][1] is None
    assert "rejected" in writer.calls[1][1] and "999.99" in writer.calls[1][1]


def test_two_invented_replies_fall_back_to_the_template():
    facts = facts_for()
    bad = reply(facts, headline="Spend rose by $999.99 today.")
    exp = explain(facts, FakeWriter([bad, bad]))
    assert exp.source == "template" and exp.fallback_reason == "output failed validation"
    assert exp.attempts == 2 and len(exp.rejected) == 2
    assert "999.99" not in exp.headline


def test_malformed_and_wrong_shape_replies_are_rejected():
    facts = facts_for()
    for raw in ("not json", "[]", json.dumps({"headline": "x"}), reply(facts, causes=[])):
        data, problems = check_output(raw, facts)
        assert data is None and problems, raw


def test_wrong_cause_ids_are_rejected():
    facts = facts_for()
    wrong = [{"id": "c9", "explanation": "x", "fix": "y"}]
    assert check_output(reply(facts, causes=wrong), facts)[0] is None


def test_writer_error_falls_back_with_the_reason():
    class Down:
        prompt_version = "x"
        last_usage = 0

        def write(self, facts, feedback=None):
            raise WriterError("API down")

    exp = explain(facts_for(), Down())
    assert exp.source == "template" and "API down" in exp.fallback_reason


def test_ai_off_uses_the_template_without_a_writer():
    for w in (None, TemplateWriter()):
        exp = explain(facts_for(), w)
        assert exp.source == "template" and exp.fallback_reason == "AI is off"


def test_exhausted_token_budget_stops_calls_entirely():
    writer = FakeWriter()
    exp = explain(facts_for(), writer, budget=TokenBudget(limit=100, used=100))
    assert exp.source == "template" and "budget" in exp.fallback_reason and writer.calls == []


def test_budget_is_charged_for_each_call():
    facts = facts_for()
    budget = TokenBudget(limit=10_000)
    explain(facts, FakeWriter([reply(facts)], usage=700), budget=budget)
    assert budget.used == 700


def test_results_are_cached_by_fact_sheet_hash():
    facts = facts_for()
    writer, cache = FakeWriter(), {}
    first = explain(facts, writer, cache=cache)
    second = explain(facts, writer, cache=cache)
    assert len(writer.calls) == 1 and not first.cached and second.cached
    assert second.headline == first.headline
    other = facts_for("nat_traffic")
    explain(other, writer, cache=cache)
    assert len(writer.calls) == 2


def test_fact_sheet_hash_is_stable_and_sensitive():
    facts = facts_for()
    assert facts_hash(facts) == facts_hash(json.loads(json.dumps(facts)))
    changed = json.loads(json.dumps(facts))
    changed["total_spend"] = "$1.00"
    assert facts_hash(facts) != facts_hash(changed)


def test_unconfirmed_cause_is_stated_plainly_in_the_template():
    facts = facts_for("nat_traffic")
    text = json.dumps(template_text(facts))
    assert "not confirmed" in text.lower()


def test_fact_sheet_holds_no_account_ids_or_raw_logs():
    blob = json.dumps(facts_for("new_big_instance", with_waste=True))
    assert "123456789012" not in blob and "EventId" not in blob and "evt-" not in blob


# ----------------------------------------------------------------- the Claude writer


class FakeClient:
    def __init__(self, response=None, error=None):
        self.calls = []
        self._response, self._error = response, error
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._response


def response(text="{}", stop="end_turn", tokens=(100, 50)):
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


def test_anthropic_writer_sends_a_clean_request_and_reads_the_text_block():
    facts = facts_for()
    client = FakeClient(response(reply(facts)))
    writer = AnthropicWriter(client, model="claude-opus-5-5")
    raw = writer.write(facts)
    (call,) = client.calls
    assert call["model"] == "claude-opus-5-5"
    assert "temperature" not in call and "top_p" not in call and "thinking" not in call
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["messages"][-1]["role"] == "user"  # no assistant prefill
    assert "Fact sheet" in call["messages"][0]["content"]
    assert writer.last_usage == 150 and check_output(raw, facts)[0] is not None


def test_anthropic_writer_includes_validator_feedback_on_retry():
    client = FakeClient(response("{}"))
    AnthropicWriter(client).write(facts_for(), "Your previous answer was rejected: bad number")
    assert "rejected: bad number" in client.calls[0]["messages"][0]["content"]


@pytest.mark.parametrize("stop, expect", [("refusal", "declined"), ("max_tokens", "cut off")])
def test_anthropic_writer_turns_bad_stop_reasons_into_writer_errors(stop, expect):
    with pytest.raises(WriterError, match=expect):
        AnthropicWriter(FakeClient(response("{}", stop))).write(facts_for())


def test_anthropic_writer_turns_api_errors_into_writer_errors_and_pipeline_falls_back():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.APIConnectionError(request=req)
    writer = AnthropicWriter(FakeClient(error=err))
    with pytest.raises(WriterError):
        writer.write(facts_for())
    exp = explain(facts_for(), writer)
    assert exp.source == "template" and "APIConnectionError" in exp.fallback_reason


def test_anthropic_writer_default_model_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("COSTLENS_MODEL", "claude-sonnet-5-5")
    assert AnthropicWriter(FakeClient()).model == "claude-sonnet-5-5"
    monkeypatch.delenv("COSTLENS_MODEL")
    assert AnthropicWriter(FakeClient()).model == "claude-opus-5-5"
