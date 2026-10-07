"""Match a cost cause to the CloudTrail event that probably produced it.

Events are the dicts returned by `cloudtrail:LookupEvents` (EventName, EventTime, Username,
Resources[{ResourceType, ResourceName}]). Each match carries a confidence label:

* ``exact``     the event names the cause's own resource ID or Auto Scaling group
* ``time_only`` an event of a relevant kind happened in the window, but not on that resource

No match means "cause not confirmed". The text layer must say so rather than guess.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

# Events that can start or change spend, by service. Unlisted services use GENERIC.
RELEVANT_EVENTS: dict[str, frozenset[str]] = {
    "AmazonEC2": frozenset(
        {
            "RunInstances",
            "StartInstances",
            "ModifyInstanceAttribute",
            "CreateNatGateway",
            "CreateVolume",
            "AllocateAddress",
            "UpdateAutoScalingGroup",
            "CreateAutoScalingGroup",
            "SetDesiredCapacity",
            "PutScalingPolicy",
            "CreateRoute",
            "ReplaceRoute",
        }
    ),
    "AmazonRDS": frozenset(
        {"CreateDBInstance", "ModifyDBInstance", "RestoreDBInstanceFromDBSnapshot"}
    ),
    "AmazonES": frozenset(
        {"CreateElasticsearchDomain", "CreateDomain", "UpdateElasticsearchDomainConfig"}
    ),
    "AmazonS3": frozenset(
        {"PutBucketLifecycleConfiguration", "PutBucketReplication", "CreateBucket"}
    ),
}
GENERIC = frozenset({"RunInstances", "CreateDBInstance", "ModifyDBInstance", "CreateDomain"})

DEFAULT_PAD = timedelta(hours=3)


@dataclass(frozen=True, slots=True)
class ContextMatch:
    event_name: str
    actor: str
    time: datetime
    resource: str | None
    confidence: str  # exact | time_only
    other_candidates: int  # further events at the same confidence that were not chosen


def window_for_day(day: date, pad: timedelta = DEFAULT_PAD) -> tuple[datetime, datetime]:
    """Search window for a change first seen on `day`: that day, padded by `pad` each side."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return start - pad, start + timedelta(days=1) + pad


def _names_match(event_resource: str, wanted: str) -> bool:
    if not event_resource or not wanted:
        return False
    if event_resource == wanted:
        return True
    # ARNs end in ".../name" or ":name"; allow a bare name to match its ARN
    return wanted.endswith(("/" + event_resource, ":" + event_resource)) or event_resource.endswith(
        ("/" + wanted, ":" + wanted)
    )


def _as_utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def match_cause(
    service: str,
    resource_ids: Iterable[str],
    asg: str,
    window: tuple[datetime, datetime],
    events: Sequence[dict],
) -> ContextMatch | None:
    """Find the best event for a cause. Exact resource matches beat time-only matches."""
    wanted = {r for r in resource_ids if r}
    if asg:
        wanted.add(asg)
    start, end = window
    relevant = RELEVANT_EVENTS.get(service, GENERIC)
    exact: list[tuple[dict, str]] = []
    timed: list[dict] = []
    for ev in events:
        t = _as_utc(ev["EventTime"])
        if not (start <= t <= end):
            continue
        hit = next(
            (
                res["ResourceName"]
                for res in ev.get("Resources", [])
                if any(_names_match(res.get("ResourceName", ""), w) for w in wanted)
            ),
            None,
        )
        if hit is not None:
            exact.append((ev, hit))
        elif ev["EventName"] in relevant:
            timed.append(ev)

    def closeness(ev: dict) -> float:
        return abs((_as_utc(ev["EventTime"]) - start - DEFAULT_PAD).total_seconds())

    if exact:
        exact.sort(key=lambda p: (p[0]["EventName"] not in relevant, closeness(p[0])))
        ev, hit = exact[0]
        return _build(ev, hit, "exact", len(exact) - 1)
    if timed:
        timed.sort(key=closeness)
        return _build(timed[0], None, "time_only", len(timed) - 1)
    return None


def _build(ev: dict, resource: str | None, confidence: str, others: int) -> ContextMatch:
    return ContextMatch(
        ev["EventName"],
        ev.get("Username", "unknown"),
        _as_utc(ev["EventTime"]),
        resource,
        confidence,
        others,
    )
