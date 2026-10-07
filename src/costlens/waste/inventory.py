"""Read-only snapshot of the resources the waste rules look at.

Everything here is a Describe* call. Nothing in CostLens writes to AWS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Volume:
    id: str
    state: str  # available | in-use | ...
    size_gb: int
    created: datetime
    tags: dict[str, str]
    instance_id: str = ""


@dataclass(frozen=True, slots=True)
class Address:
    allocation_id: str
    public_ip: str
    associated: bool
    tags: dict[str, str]


@dataclass(frozen=True, slots=True)
class Instance:
    id: str
    state: str  # running | stopped | ...
    instance_type: str
    launched: datetime
    tags: dict[str, str]
    volume_ids: tuple[str, ...] = ()
    stopped_at: datetime | None = None
    asg: str = ""


@dataclass(frozen=True, slots=True)
class NatGateway:
    id: str
    state: str
    created: datetime
    tags: dict[str, str]


@dataclass(frozen=True, slots=True)
class LoadBalancer:
    arn: str
    name: str
    kind: str  # application | network
    created: datetime
    tags: dict[str, str]

    @property
    def metric_dimension(self) -> str:
        """CloudWatch's LoadBalancer dimension is the ARN suffix after 'loadbalancer/'."""
        return self.arn.split("loadbalancer/", 1)[-1]


@dataclass(frozen=True, slots=True)
class AutoScalingGroup:
    name: str
    min_size: int
    max_size: int
    desired: int
    instance_ids: tuple[str, ...]
    tags: dict[str, str]


@dataclass(slots=True)
class Inventory:
    region: str
    volumes: list[Volume] = field(default_factory=list)
    addresses: list[Address] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    nat_gateways: list[NatGateway] = field(default_factory=list)
    load_balancers: list[LoadBalancer] = field(default_factory=list)
    asgs: list[AutoScalingGroup] = field(default_factory=list)
    # When a volume was detached, if CloudTrail told us. Falls back to creation time.
    volume_detached_at: dict[str, datetime] = field(default_factory=dict)


def _tags(raw: list[dict[str, str]] | None) -> dict[str, str]:
    return {t["Key"]: t.get("Value", "") for t in raw or []}


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


_STOP_RE = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) GMT\)")


def parse_stop_time(reason: str | None) -> datetime | None:
    """Read the stop time from StateTransitionReason, e.g. '... (2026-09-01 10:00:00 GMT)'."""
    m = _STOP_RE.search(reason or "")
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def collect(session: Any) -> Inventory:
    """Build an Inventory from a boto3 Session using Describe* calls only."""
    region = session.region_name or "unknown"
    ec2 = session.client("ec2")
    inv = Inventory(region)

    for page in ec2.get_paginator("describe_volumes").paginate():
        for v in page["Volumes"]:
            attached = (v.get("Attachments") or [{}])[0].get("InstanceId", "")
            inv.volumes.append(
                Volume(
                    v["VolumeId"],
                    v["State"],
                    v["Size"],
                    _utc(v["CreateTime"]),
                    _tags(v.get("Tags")),
                    attached,
                )
            )

    for a in ec2.describe_addresses()["Addresses"]:
        inv.addresses.append(
            Address(
                a.get("AllocationId", a.get("PublicIp", "")),
                a.get("PublicIp", ""),
                bool(a.get("AssociationId") or a.get("InstanceId") or a.get("NetworkInterfaceId")),
                _tags(a.get("Tags")),
            )
        )

    for page in ec2.get_paginator("describe_instances").paginate():
        for res in page["Reservations"]:
            for i in res["Instances"]:
                state = i["State"]["Name"]
                if state in ("terminated", "shutting-down"):
                    continue
                tags = _tags(i.get("Tags"))
                vols = tuple(
                    m["Ebs"]["VolumeId"] for m in i.get("BlockDeviceMappings", []) if "Ebs" in m
                )
                inv.instances.append(
                    Instance(
                        i["InstanceId"],
                        state,
                        i["InstanceType"],
                        _utc(i["LaunchTime"]),
                        tags,
                        vols,
                        parse_stop_time(i.get("StateTransitionReason"))
                        if state == "stopped"
                        else None,
                        tags.get("aws:autoscaling:groupName", ""),
                    )
                )

    for page in ec2.get_paginator("describe_nat_gateways").paginate():
        for n in page["NatGateways"]:
            if n["State"] in ("deleted", "deleting", "failed"):
                continue
            inv.nat_gateways.append(
                NatGateway(
                    n["NatGatewayId"], n["State"], _utc(n["CreateTime"]), _tags(n.get("Tags"))
                )
            )

    elb = session.client("elbv2")
    for page in elb.get_paginator("describe_load_balancers").paginate():
        for lb in page["LoadBalancers"]:
            if lb["Type"] not in ("application", "network"):
                continue
            tag_desc = elb.describe_tags(ResourceArns=[lb["LoadBalancerArn"]])["TagDescriptions"]
            inv.load_balancers.append(
                LoadBalancer(
                    lb["LoadBalancerArn"],
                    lb["LoadBalancerName"],
                    lb["Type"],
                    _utc(lb["CreatedTime"]),
                    _tags(tag_desc[0]["Tags"]) if tag_desc else {},
                )
            )

    asg = session.client("autoscaling")
    for page in asg.get_paginator("describe_auto_scaling_groups").paginate():
        for g in page["AutoScalingGroups"]:
            inv.asgs.append(
                AutoScalingGroup(
                    g["AutoScalingGroupName"],
                    g["MinSize"],
                    g["MaxSize"],
                    g["DesiredCapacity"],
                    tuple(i["InstanceId"] for i in g.get("Instances", [])),
                    {t["Key"]: t.get("Value", "") for t in g.get("Tags", [])},
                )
            )
    return inv
