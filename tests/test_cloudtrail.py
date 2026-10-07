from datetime import UTC, date, datetime, timedelta

from costlens.cloudtrail import match_cause, window_for_day
from costlens.synth import INCIDENT_KINDS, generate

DAY = date(2026, 10, 6)
WINDOW = window_for_day(DAY)
T = datetime(2026, 10, 6, 2, 14, tzinfo=UTC)


def ev(name, resource, actor="alice", at=T):
    return {
        "EventName": name,
        "EventTime": at,
        "Username": actor,
        "Resources": [{"ResourceType": "x", "ResourceName": resource}] if resource else [],
    }


def test_exact_match_on_the_resource_id_names_the_actor():
    m = match_cause(
        "AmazonEC2", ["i-0abc"], "", WINDOW, [ev("RunInstances", "i-0abc", "deploy-bot")]
    )
    assert (m.confidence, m.actor, m.event_name, m.resource) == (
        "exact",
        "deploy-bot",
        "RunInstances",
        "i-0abc",
    )


def test_exact_match_beats_a_relevant_event_on_another_resource():
    events = [ev("RunInstances", "i-other", "bob"), ev("RunInstances", "i-0abc", "alice")]
    m = match_cause("AmazonEC2", ["i-0abc"], "", WINDOW, events)
    assert m.actor == "alice" and m.confidence == "exact"


def test_asg_name_matches_an_autoscaling_event():
    m = match_cause(
        "AmazonEC2", [], "api-prod-asg", WINDOW, [ev("UpdateAutoScalingGroup", "api-prod-asg")]
    )
    assert m.confidence == "exact"


def test_time_only_match_when_no_event_names_the_resource():
    m = match_cause("AmazonEC2", ["i-0abc"], "", WINDOW, [ev("RunInstances", "i-other", "bob")])
    assert m.confidence == "time_only" and m.actor == "bob" and m.resource is None


def test_irrelevant_events_do_not_match_at_all():
    assert (
        match_cause("AmazonEC2", ["i-0abc"], "", WINDOW, [ev("PutParameter", "/app/config")])
        is None
    )


def test_events_outside_the_window_are_ignored():
    late = ev("RunInstances", "i-0abc", at=T + timedelta(days=2))
    early = ev("RunInstances", "i-0abc", at=T - timedelta(days=2))
    assert match_cause("AmazonEC2", ["i-0abc"], "", WINDOW, [late, early]) is None


def test_a_bare_name_matches_its_arn():
    arn = "arn:aws:es:ap-south-1:123456789012:domain/search-prod"
    m = match_cause("AmazonES", [arn], "", WINDOW, [ev("CreateDomain", "search-prod", "carol")])
    assert m.confidence == "exact" and m.actor == "carol"


def test_no_events_means_not_confirmed():
    assert match_cause("AmazonEC2", ["i-0abc"], "", WINDOW, []) is None


def test_naive_datetimes_are_treated_as_utc():
    naive = ev("RunInstances", "i-0abc", at=datetime(2026, 10, 6, 2, 14))
    assert match_cause("AmazonEC2", ["i-0abc"], "", WINDOW, [naive]).confidence == "exact"


def test_synthetic_incidents_with_an_actor_are_matched_exactly_despite_decoys():
    for kind in INCIDENT_KINDS:
        ds = generate(seed=5, incident=kind)
        inc = ds.incidents[0]
        resources = [v for k, v in inc.expected.items() if k == "resource_id"]
        asg = inc.expected.get("asg", "")
        if kind == "new_service":  # the expected scope is the service only; use its resource
            resources = [r.resource_id for r in ds.rows if r.service == inc.expected["service"]][:1]
        m = match_cause(
            inc.expected["service"], resources, asg, window_for_day(inc.start_day), ds.cloudtrail
        )
        if inc.expected_actor:
            assert m and m.confidence == "exact" and m.actor == inc.expected_actor, kind
        else:
            assert m is None or m.confidence == "time_only", kind
