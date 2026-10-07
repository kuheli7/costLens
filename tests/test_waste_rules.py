from datetime import UTC, date, datetime, timedelta

from costlens.models import CostRow
from costlens.waste import FixtureMetrics, Inventory, PriceBook, WasteConfig, run_all
from costlens.waste.inventory import (
    Address,
    AutoScalingGroup,
    Instance,
    LoadBalancer,
    NatGateway,
    Volume,
    parse_stop_time,
)

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=60)


def inv(**kw):
    return Inventory("ap-south-1", **kw)


def findings(i, metrics=None, prices=None, cfg=None):
    f, s = run_all(i, metrics or FixtureMetrics(), prices, cfg, NOW)
    return f, s


def cpu_series(value, hours=14 * 24, spikes=()):
    out = [value] * hours
    for h in spikes:
        out[h] = 90.0
    return out


def idle_metrics(iid, cpu, net_bytes_per_day=1_000_000, days=14):
    m = FixtureMetrics()
    m.set("AWS/EC2", "CPUUtilization", {"InstanceId": iid}, "Average", cpu)
    m.set("AWS/EC2", "NetworkIn", {"InstanceId": iid}, "Sum", [net_bytes_per_day] * days)
    m.set("AWS/EC2", "NetworkOut", {"InstanceId": iid}, "Sum", [net_bytes_per_day] * days)
    return m


def instance(iid="i-1", state="running", launched=OLD, tags=None, **kw):
    return Instance(iid, state, "m5.large", launched, tags or {}, **kw)


# ----------------------------------------------------------------- idle EC2


def test_idle_instance_is_flagged_with_evidence_and_a_stop_command():
    f, _ = findings(inv(instances=[instance()]), idle_metrics("i-1", cpu_series(1.0)))
    (x,) = f
    assert x.rule == "idle_ec2" and x.resource_id == "i-1"
    assert x.evidence["p95_cpu_pct"] == 1.0 and "stop-instances" in x.fix
    assert x.savings_source == "estimate" and x.monthly_savings_cents == 7000


def test_busy_instance_is_not_flagged():
    f, _ = findings(inv(instances=[instance()]), idle_metrics("i-1", cpu_series(35.0)))
    assert f == []


def test_idle_cpu_but_real_network_traffic_is_not_flagged():
    m = idle_metrics("i-1", cpu_series(1.0), net_bytes_per_day=80_000_000)
    assert findings(inv(instances=[instance()]), m)[0] == []


def test_instance_younger_than_the_lookback_is_skipped_and_recorded():
    young = instance(launched=NOW - timedelta(days=3))
    f, s = findings(inv(instances=[young]), idle_metrics("i-1", cpu_series(1.0)))
    assert f == [] and "lookback" in s[0].reason


def test_batch_workload_busy_one_hour_a_day_is_not_called_idle():
    spikes = [d * 24 + 3 for d in range(14)]  # 1 busy hour per day = ~4% of datapoints
    m = idle_metrics("i-1", cpu_series(1.0, spikes=spikes))
    f, s = findings(inv(instances=[instance()]), m)
    assert f == [] and "batch" in s[0].reason


def test_missing_metrics_are_not_treated_as_idle():
    f, s = findings(inv(instances=[instance()]), FixtureMetrics())
    assert f == [] and "not enough" in s[0].reason


def test_savings_come_from_cur_when_the_resource_has_cost_rows():
    rows = [
        CostRow(date(2026, 10, d), "AmazonEC2", "r", "BoxUsage", "i-1", "t", "", 24, 300)
        for d in range(1, 8)
    ]
    f, _ = findings(
        inv(instances=[instance()]), idle_metrics("i-1", cpu_series(1.0)), PriceBook(rows)
    )
    assert f[0].savings_source == "cur" and f[0].monthly_savings_cents == 9000  # 300c x 30


def test_asg_members_are_left_to_the_group_rule():
    m = idle_metrics("i-1", cpu_series(1.0))
    assert findings(inv(instances=[instance(asg="g")]), m)[0] == []


# ----------------------------------------------------------------- ignore tags


def test_ignore_tag_with_future_expiry_suppresses_the_finding():
    tagged = instance(tags={"costlens:ignore": "true", "costlens:ignore-until": "2026-12-31"})
    f, s = findings(inv(instances=[tagged]), idle_metrics("i-1", cpu_series(1.0)))
    assert f == [] and "until 2026-12-31" in s[0].reason


def test_expired_ignore_tag_no_longer_suppresses():
    tagged = instance(tags={"costlens:ignore": "true", "costlens:ignore-until": "2026-09-01"})
    f, _ = findings(inv(instances=[tagged]), idle_metrics("i-1", cpu_series(1.0)))
    assert len(f) == 1


def test_ignore_tag_without_expiry_suppresses_but_says_to_set_one():
    tagged = instance(tags={"costlens:ignore": "true"})
    f, s = findings(inv(instances=[tagged]), idle_metrics("i-1", cpu_series(1.0)))
    assert f == [] and "ignore-until" in s[0].reason


def test_allow_list_suppresses():
    cfg = WasteConfig(allow_list=frozenset({"i-1"}))
    f, s = findings(inv(instances=[instance()]), idle_metrics("i-1", cpu_series(1.0)), cfg=cfg)
    assert f == [] and s[0].reason == "on the allow-list"


# ----------------------------------------------------------------- oversized ASG


def asg_metrics(name, cpu, desired):
    m = FixtureMetrics()
    m.set("AWS/EC2", "CPUUtilization", {"AutoScalingGroupName": name}, "Average", cpu)
    m.set(
        "AWS/AutoScaling",
        "GroupDesiredCapacity",
        {"AutoScalingGroupName": name},
        "Average",
        desired,
    )
    return m


def asg_inventory(min_size, desired):
    ids = tuple(f"i-{n}" for n in range(desired))
    members = [instance(i, asg="g") for i in ids]
    g = AutoScalingGroup("g", min_size, 12, desired, ids, {})
    return inv(instances=members, asgs=[g])


def test_asg_that_never_scales_in_and_runs_cold_is_flagged():
    m = asg_metrics("g", [10.0] * 168, [8.0] * 168)
    f, _ = findings(asg_inventory(3, 8), m, cfg=WasteConfig())
    (x,) = f
    assert x.rule == "oversized_asg" and x.evidence["never_scales_in"] is True
    assert x.evidence["suggested_capacity"] == 2 and "scale-in" in x.fix
    assert x.monthly_savings_cents == 6 * 7000


def test_asg_with_low_cpu_and_high_min_gets_a_min_size_fix():
    desired = [3.0, 4.0, 3.0, 5.0] * 42
    m = asg_metrics("g", [8.0] * 168, desired)
    f, _ = findings(asg_inventory(3, 3), m)
    (x,) = f
    assert "--min-size 1" in x.fix and x.evidence["never_scales_in"] is False


def test_busy_asg_is_left_alone():
    m = asg_metrics("g", [65.0] * 168, [8.0] * 168)
    assert findings(asg_inventory(3, 8), m)[0] == []


# ----------------------------------------------------------------- EBS, EIP, stopped


def test_unattached_volume_older_than_a_week_is_flagged_and_priced_by_size():
    v = Volume("vol-1", "available", 500, OLD, {})
    f, _ = findings(inv(volumes=[v]))
    (x,) = f
    assert x.rule == "unattached_ebs" and x.monthly_savings_cents == 4000
    assert "create-snapshot" in x.fix and x.evidence["age_measured_from"] == "creation"


def test_attached_and_recently_detached_volumes_are_not_flagged():
    attached = Volume("vol-1", "in-use", 100, OLD, {}, "i-1")
    detached_yesterday = Volume("vol-2", "available", 100, OLD, {})
    i = inv(volumes=[attached, detached_yesterday])
    i.volume_detached_at["vol-2"] = NOW - timedelta(days=1)
    f, s = findings(i)
    assert f == [] and "less than 7 days" in s[0].reason


def test_unassociated_elastic_ip_is_flagged_and_associated_is_not():
    f, _ = findings(
        inv(
            addresses=[
                Address("eipalloc-1", "1.2.3.4", False, {}),
                Address("eipalloc-2", "5.6.7.8", True, {}),
            ]
        )
    )
    assert [x.resource_id for x in f] == ["eipalloc-1"] and "release-address" in f[0].fix


def test_stopped_instance_with_attached_storage_is_flagged_after_14_days():
    stopped = instance(
        "i-s", "stopped", volume_ids=("vol-a", "vol-b"), stopped_at=NOW - timedelta(days=30)
    )
    vols = [
        Volume("vol-a", "in-use", 300, OLD, {}, "i-s"),
        Volume("vol-b", "in-use", 200, OLD, {}, "i-s"),
    ]
    f, _ = findings(inv(instances=[stopped], volumes=vols))
    (x,) = f
    assert x.rule == "stopped_instance_storage" and x.evidence["storage_gb"] == 500
    assert x.monthly_savings_cents == 4000 and "terminate-instances" in x.fix


def test_recently_stopped_instance_is_not_flagged():
    stopped = instance("i-s", "stopped", volume_ids=("vol-a",), stopped_at=NOW - timedelta(days=2))
    f, s = findings(inv(instances=[stopped], volumes=[Volume("vol-a", "in-use", 8, OLD, {})]))
    assert f == [] and "less than 14 days" in s[0].reason


def test_parse_stop_time():
    assert parse_stop_time("User initiated (2026-09-01 10:00:00 GMT)") == datetime(
        2026, 9, 1, 10, tzinfo=UTC
    )
    assert parse_stop_time("") is None and parse_stop_time(None) is None


# ----------------------------------------------------------------- NAT / load balancers


def test_idle_nat_gateway_is_flagged_and_busy_one_is_not():
    idle = NatGateway("nat-idle", "available", OLD, {})
    busy = NatGateway("nat-busy", "available", OLD, {})
    m = FixtureMetrics()
    m.set(
        "AWS/NATGateway", "BytesOutToDestination", {"NatGatewayId": "nat-idle"}, "Sum", [1000.0] * 7
    )
    m.set("AWS/NATGateway", "BytesOutToDestination", {"NatGatewayId": "nat-busy"}, "Sum", [5e9] * 7)
    f, _ = findings(inv(nat_gateways=[idle, busy]), m)
    (x,) = f
    assert x.resource_id == "nat-idle" and "gateway endpoint" in x.fix


def test_idle_application_load_balancer_is_flagged():
    arn = "arn:aws:elasticloadbalancing:ap-south-1:1:loadbalancer/app/web/abc123"
    lb = LoadBalancer(arn, "web", "application", OLD, {})
    m = FixtureMetrics()
    m.set(
        "AWS/ApplicationELB", "RequestCount", {"LoadBalancer": "app/web/abc123"}, "Sum", [0.0] * 7
    )
    f, _ = findings(inv(load_balancers=[lb]), m)
    assert f[0].resource_id == "web" and "delete-load-balancer" in f[0].fix


def test_findings_are_ranked_by_savings():
    v = Volume("vol-1", "available", 10, OLD, {})
    a = Address("eipalloc-1", "1.2.3.4", False, {})
    big = Volume("vol-2", "available", 1000, OLD, {})
    f, _ = findings(inv(volumes=[v, big], addresses=[a]))
    savings = [x.monthly_savings_cents for x in f]
    assert savings == sorted(savings, reverse=True) and f[0].resource_id == "vol-2"
