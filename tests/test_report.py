from datetime import UTC, date, datetime, timedelta

import pytest

from costlens.report import analyze_day
from costlens.synth import ASG_NAME, INCIDENT_KINDS, generate

LATE = datetime(2026, 10, 20, tzinfo=UTC)  # long after the day, so nothing is provisional


def report_for(kind, seed=7, age=0):
    ds = generate(seed=seed, incident=kind, incident_age=age)
    return ds, analyze_day(ds.rows, ds.last_day, ds.cloudtrail, now=LATE)


@pytest.mark.parametrize("kind", INCIDENT_KINDS)
def test_top_cause_contains_the_injected_incident(kind):
    ds, rep = report_for(kind)
    inc = ds.incidents[0]
    assert rep.significant and rep.causes
    top = rep.causes[0]
    for dim, value in inc.expected.items():
        assert top.scope.get(dim) == value, (kind, dim, top.scope)


def test_asg_scale_out_is_a_group_cause_with_contributors_and_a_matched_actor():
    ds, rep = report_for("asg_scale_out")
    top = rep.causes[0]
    assert top.scope["asg"] == ASG_NAME and len(top.contributors) == 3
    assert top.delta_cents == 1200 and top.volume_cents == 1200 and top.rate_cents == 0
    assert top.match.confidence == "exact" and top.match.actor == "deploy-bot"
    assert top.match.event_name == "UpdateAutoScalingGroup"


def test_new_big_instance_names_the_actor_and_not_the_decoy():
    ds, rep = report_for("new_big_instance")
    m = rep.causes[0].match
    assert m.actor == "alice" and m.confidence == "exact" and m.event_name == "RunInstances"


def test_rate_change_is_labelled_as_rate_not_volume():
    ds, rep = report_for("rate_change")
    top = rep.causes[0]
    assert top.rate_cents > 0 and top.volume_cents == 0


def test_cause_without_a_cloudtrail_event_has_no_match():
    ds, rep = report_for("nat_traffic")
    assert rep.causes[0].match is None


def test_persisting_incident_is_still_explained_and_onset_is_found():
    ds, rep = report_for("new_big_instance", age=2)
    top = rep.causes[0]
    assert top.scope["resource_id"] == ds.incidents[0].expected["resource_id"]
    assert top.onset == ds.last_day - timedelta(days=2)
    assert top.match and top.match.actor == "alice"


def test_quiet_day_is_not_significant_and_has_no_causes():
    ds = generate(seed=2)
    rep = analyze_day(ds.rows, ds.last_day, now=LATE)
    assert not rep.significant and rep.causes == []
    assert abs(rep.pct) < 0.25


def test_amounts_in_the_report_are_consistent():
    ds, rep = report_for("transfer_spike")
    assert rep.total_cents - rep.baseline_cents == rep.delta_cents
    assert rep.causes[0].delta_cents <= rep.delta_cents + 10  # offsetting noise is tiny here


def test_untagged_spend_is_reported_with_the_resource():
    ds, rep = report_for("asg_scale_out")
    assert rep.untagged.cents > 0 and 0 < rep.untagged.share < 0.2
    assert len(rep.untagged.resources) >= 1


def test_tax_is_reported_separately_and_not_blamed():
    ds, rep = report_for("new_big_instance")
    assert rep.non_usage["Tax"] > 0
    assert all(c.scope.get("service") != "Tax" for c in rep.causes)


def test_recent_day_is_marked_provisional():
    ds = generate(seed=1, incident="new_big_instance")
    soon = datetime.combine(ds.last_day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    rep = analyze_day(ds.rows, ds.last_day, ds.cloudtrail, now=soon + timedelta(hours=9))
    assert any("provisional" in w for w in rep.warnings)


def test_reconciliation_failure_is_surfaced_as_incomplete_data():
    ds = generate(seed=1, incident="new_big_instance")
    cur_total = sum(r.cost_cents for r in ds.rows if r.day == ds.last_day)
    ok = analyze_day(ds.rows, ds.last_day, now=LATE, ce_total_cents=cur_total + 30)
    bad = analyze_day(ds.rows, ds.last_day, now=LATE, ce_total_cents=round(cur_total * 1.5))
    assert ok.reconciliation.ok and not any("incomplete" in w for w in ok.warnings)
    assert not bad.reconciliation.ok and any("incomplete" in w for w in bad.warnings)


def test_first_of_month_support_fee_is_not_reported_as_a_cause():
    ds = generate(seed=4)
    first = date(2026, 10, 1)
    rep = analyze_day(ds.rows, first, now=LATE)
    assert not rep.significant
    assert any("monthly fee" in w for w in rep.warnings)


def test_short_history_falls_back_and_says_so():
    ds = generate(seed=1, days=10, incident="new_big_instance")
    rep = analyze_day(ds.rows, ds.last_day, now=LATE)
    assert rep.baseline_method == "trailing"
    assert any("trailing" in w for w in rep.warnings)
