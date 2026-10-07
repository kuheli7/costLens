from datetime import UTC, datetime, timedelta

import boto3
from moto import mock_aws

from costlens.waste import CloudWatchMetrics, collect, run_all
from costlens.waste.rules import WasteConfig


def _session():
    return boto3.Session(region_name="ap-south-1")


def _ami(ec2):
    return ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]


@mock_aws
def test_collect_reads_volumes_addresses_instances_and_nat():
    s = _session()
    ec2 = s.client("ec2")
    ec2.create_volume(
        AvailabilityZone="ap-south-1a",
        Size=100,
        TagSpecifications=[
            {"ResourceType": "volume", "Tags": [{"Key": "costlens:ignore", "Value": "true"}]}
        ],
    )
    ec2.allocate_address(Domain="vpc")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
    eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    ec2.create_nat_gateway(SubnetId=subnet, AllocationId=eip)
    run = ec2.run_instances(
        ImageId=_ami(ec2), MinCount=1, MaxCount=1, InstanceType="t3.micro", SubnetId=subnet
    )
    iid = run["Instances"][0]["InstanceId"]
    ec2.stop_instances(InstanceIds=[iid])

    inv = collect(s)

    assert inv.region == "ap-south-1"
    vols = {v.id: v for v in inv.volumes}
    unattached = [v for v in inv.volumes if v.state == "available"]
    assert len(unattached) == 1 and unattached[0].size_gb == 100
    assert unattached[0].tags == {"costlens:ignore": "true"}
    assert len(vols) == 2  # the instance's root volume plus ours
    assert sorted(a.associated for a in inv.addresses) == [False, True]  # NAT holds one
    assert len(inv.nat_gateways) == 1
    (inst,) = inv.instances
    assert inst.id == iid and inst.state == "stopped" and inst.volume_ids


@mock_aws
def test_collect_reads_load_balancers_and_autoscaling_groups():
    s = _session()
    ec2, elb, asg = s.client("ec2"), s.client("elbv2"), s.client("autoscaling")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    sub_a = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a")[
        "Subnet"
    ]["SubnetId"]
    sub_b = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.2.0/24", AvailabilityZone="ap-south-1b")[
        "Subnet"
    ]["SubnetId"]
    elb.create_load_balancer(
        Name="web",
        Subnets=[sub_a, sub_b],
        Type="application",
        Tags=[{"Key": "team", "Value": "web"}],
    )
    asg.create_launch_configuration(
        LaunchConfigurationName="lc", ImageId=_ami(ec2), InstanceType="t3.micro"
    )
    asg.create_auto_scaling_group(
        AutoScalingGroupName="api",
        LaunchConfigurationName="lc",
        MinSize=2,
        MaxSize=6,
        DesiredCapacity=3,
        VPCZoneIdentifier=sub_a,
    )

    inv = collect(s)

    (lb,) = inv.load_balancers
    assert lb.name == "web" and lb.kind == "application" and lb.tags == {"team": "web"}
    assert lb.metric_dimension.startswith("app/web/")
    (g,) = inv.asgs
    assert (g.name, g.min_size, g.max_size, g.desired) == ("api", 2, 6, 3)
    assert len(g.instance_ids) == 3
    assert {i.asg for i in inv.instances} == {"api"}


@mock_aws
def test_cloudwatch_metrics_round_trip():
    s = _session()
    cw = s.client("cloudwatch")
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    for h in range(1, 6):
        cw.put_metric_data(
            Namespace="AWS/EC2",
            MetricData=[
                {
                    "MetricName": "CPUUtilization",
                    "Dimensions": [{"Name": "InstanceId", "Value": "i-1"}],
                    "Timestamp": now - timedelta(hours=h),
                    "Value": float(h),
                    "Unit": "Percent",
                }
            ],
        )
    series = CloudWatchMetrics(cw).series(
        "AWS/EC2",
        "CPUUtilization",
        {"InstanceId": "i-1"},
        "Average",
        3600,
        1,
        now + timedelta(hours=1),
    )
    assert sorted(series) == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert series == [5.0, 4.0, 3.0, 2.0, 1.0]  # oldest first


@mock_aws
def test_end_to_end_inventory_to_findings_on_an_account_with_orphans():
    s = _session()
    ec2 = s.client("ec2")
    ec2.create_volume(AvailabilityZone="ap-south-1a", Size=500)
    ec2.allocate_address(Domain="vpc")
    inv = collect(s)
    # moto stamps resources 'now'; look from 30 days later so they are old enough to judge
    later = datetime.now(UTC) + timedelta(days=30)
    findings, _ = run_all(
        inv, CloudWatchMetrics(s.client("cloudwatch")), now=later, cfg=WasteConfig()
    )
    assert {f.rule for f in findings} == {"unattached_ebs", "unattached_eip"}
    assert all(f.monthly_savings_cents > 0 for f in findings)
