"""The validator must accept ordinary phrasings of real facts, not only the template's wording."""

import json
from datetime import UTC, datetime

import pytest

from costlens.explain import FakeWriter, template_text
from costlens.factsheet import build
from costlens.report import analyze_day
from costlens.synth import generate
from costlens.validate import Allowed, validate_text
from costlens.writer_eval import evaluate, fact_sheets, render

NOW = datetime(2026, 12, 1, tzinfo=UTC)


def facts():
    ds = generate(seed=7, incident="asg_scale_out")
    return build(analyze_day(ds.rows, ds.last_day, ds.cloudtrail, now=NOW))


@pytest.mark.parametrize(
    "sentence",
    [
        "The extra spend came from an m5.large instance group.",
        "It started at 2:14 UTC.",
        "That is {share} percent of the change.",
        "Spend rose on 6 October.",
        "Spend rose by about $12 on Tuesday 6 October 2026.",
        "The api-prod-asg group grew, and deploy-bot changed it.",
        "UpdateAutoScalingGroup was called by deploy-bot.",
    ],
)
def test_ordinary_phrasings_of_real_facts_are_accepted(sentence):
    f = facts()
    share = f["causes"][0]["share_of_total_change"].rstrip("%")
    assert validate_text(sentence.format(share=share), Allowed(f)) == [], sentence


def test_a_wrong_time_in_a_different_format_is_still_rejected():
    assert validate_text("It started at 3:14 UTC.", Allowed(facts())) == ["time not in facts: 3:14"]


def test_writer_eval_dry_run_accepts_every_scripted_answer():
    sheets = fact_sheets(per_kind=5)
    assert len(sheets) >= 30
    writer = FakeWriter([json.dumps(template_text(f)) for f in sheets], usage=100)
    rep = evaluate(writer, sheets)
    assert rep.n == len(sheets) and rep.first_try == rep.n and rep.fell_back == 0
    assert rep.tokens == 100 * rep.n and "first try" in render(rep, "dry")


def test_writer_eval_counts_retries_and_fallbacks():
    sheets = fact_sheets(per_kind=1)[:3]
    bad = [json.dumps({**template_text(f), "headline": "Up $999.99."}) for f in sheets]
    good = [json.dumps(template_text(f)) for f in sheets]
    # sheet 0: bad then good (retry); sheet 1: good; sheet 2: bad, bad (fallback)
    writer = FakeWriter([bad[0], good[0], good[1], bad[2], bad[2]])
    rep = evaluate(writer, sheets)
    assert (rep.first_try, rep.after_retry, rep.fell_back) == (1, 1, 1)
    assert rep.reasons == {"output failed validation": 1} and rep.examples
