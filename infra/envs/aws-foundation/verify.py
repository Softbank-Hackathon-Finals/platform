#!/usr/bin/env python3
"""Read back the applied T1 boundary using AWS APIs. Does not send packets or mutate resources."""

import datetime
import json
import subprocess
import sys
from pathlib import Path


def command(*args):
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def verify():
    config = command("terraform", "output", "-json", "foundation")
    region = config["region"]

    def aws(service, operation, *args):
        return command(
            "aws", "--region", region, service, operation, *args, "--output", "json"
        )

    identity = aws("sts", "get-caller-identity")
    if identity["Account"] != config["account_id"]:
        raise RuntimeError("Wrong AWS account")
    facts = {
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "account": identity["Account"],
        "region": region,
        "name": config["name"],
        "checks": [],
    }

    def check(name, condition):
        if not condition:
            raise RuntimeError("AWS readback failed: " + name)
        facts["checks"].append({"check": name, "passed": True})

    vpc_ids = [v["id"] for v in config["vpcs"].values()]
    vpcs = aws("ec2", "describe-vpcs", "--vpc-ids", *vpc_ids)["Vpcs"]
    check(
        "two available VPCs",
        len(vpcs) == 2 and all(v["State"] == "available" for v in vpcs),
    )
    peer = aws(
        "ec2",
        "describe-vpc-peering-connections",
        "--vpc-peering-connection-ids",
        config["peering_id"],
    )["VpcPeeringConnections"][0]
    check("peering active", peer["Status"]["Code"] == "active")
    check(
        "DNS resolution both directions",
        all(
            peer[side]["PeeringOptions"]["AllowDnsResolutionFromRemoteVpc"]
            for side in ["RequesterVpcInfo", "AccepterVpcInfo"]
        ),
    )
    subnets = aws(
        "ec2",
        "describe-subnets",
        "--subnet-ids",
        *[s["id"] for s in config["subnets"].values()],
    )["Subnets"]
    check(
        "four private subnets",
        len(subnets) == 4 and all(not s["MapPublicIpOnLaunch"] for s in subnets),
    )
    default_group_ids = []
    for name, vpc in config["vpcs"].items():
        own = [s for s in subnets if s["VpcId"] == vpc["id"]]
        check(name + " has two AZs", len({s["AvailabilityZone"] for s in own}) == 2)
        rt = aws(
            "ec2",
            "describe-route-tables",
            "--route-table-ids",
            config["route_tables"][name],
        )["RouteTables"][0]
        routes = [r for r in rt["Routes"] if r.get("VpcPeeringConnectionId")]
        other = config["vpcs"]["platform" if name == "source" else "source"]
        check(
            name + " peer route only",
            len(rt["Routes"]) == 2
            and len(routes) == 1
            and routes[0]["DestinationCidrBlock"] == other["cidr"]
            and routes[0]["VpcPeeringConnectionId"] == config["peering_id"]
            and routes[0]["State"] == "active",
        )
        check(
            name + " all subnets use peer route table",
            {a.get("SubnetId") for a in rt["Associations"]}
            == {s["SubnetId"] for s in own},
        )
        default = aws(
            "ec2",
            "describe-security-groups",
            "--filters",
            "Name=vpc-id,Values=" + vpc["id"],
            "Name=group-name,Values=default",
        )["SecurityGroups"][0]
        default_group_ids.append(default["GroupId"])
        check(
            name + " default SG closed",
            not default["IpPermissions"] and not default["IpPermissionsEgress"],
        )
    groups = {
        g["GroupId"]: g
        for g in aws(
            "ec2",
            "describe-security-groups",
            "--group-ids",
            *config["security_groups"].values(),
        )["SecurityGroups"]
    }
    control = config["security_groups"]["platform_control"]
    expected_egress = set()
    for name, port in [("source_api", 443), ("source_db", 5432)]:
        group = groups[config["security_groups"][name]]
        rules = group["IpPermissions"]
        check(
            name + " ingress from platform only",
            len(rules) == 1
            and rules[0]["IpProtocol"] == "tcp"
            and rules[0]["FromPort"] == port
            and rules[0]["ToPort"] == port
            and not rules[0]["IpRanges"]
            and not rules[0]["Ipv6Ranges"]
            and len(rules[0]["UserIdGroupPairs"]) == 1
            and rules[0]["UserIdGroupPairs"][0]["GroupId"] == control,
        )
        check(
            name + " cannot initiate outbound traffic", not group["IpPermissionsEgress"]
        )
        expected_egress.add((port, config["security_groups"][name]))
    egress = groups[control]["IpPermissionsEgress"]
    check(
        "platform denies inbound and only reaches two source ports",
        not groups[control]["IpPermissions"]
        and len(egress) == 2
        and all(
            r["IpProtocol"] == "tcp"
            and r["FromPort"] == r["ToPort"]
            and not r["IpRanges"]
            and not r["Ipv6Ranges"]
            and len(r["UserIdGroupPairs"]) == 1
            for r in egress
        )
        and {(r["ToPort"], r["UserIdGroupPairs"][0]["GroupId"]) for r in egress}
        == expected_egress,
    )
    logs = aws("ec2", "describe-flow-logs", "--flow-log-ids", *config["flow_log_ids"])[
        "FlowLogs"
    ]
    check(
        "flow logs active for both VPCs",
        len(logs) == 2
        and all(
            x["FlowLogStatus"] == "ACTIVE" and x["TrafficType"] == "ALL" for x in logs
        ),
    )
    expected_ids = set(
        vpc_ids
        + [s["SubnetId"] for s in subnets]
        + list(config["route_tables"].values())
        + list(config["security_groups"].values())
        + [config["peering_id"]]
        + config["flow_log_ids"]
        + default_group_ids
    )
    rules = aws(
        "ec2",
        "describe-security-group-rules",
        "--filters",
        "Name=group-id,Values=" + ",".join(config["security_groups"].values()),
    )["SecurityGroupRules"]
    expected_ids.update(r["SecurityGroupRuleId"] for r in rules)
    ec2_tags = aws(
        "ec2",
        "describe-tags",
        "--filters",
        "Name=resource-id,Values=" + ",".join(sorted(expected_ids)),
    )["Tags"]
    tagged = {}
    for tag in ec2_tags:
        tagged.setdefault(tag["ResourceId"], {})[tag["Key"]] = tag["Value"]
    check(
        "EC2 resources carry component tags",
        expected_ids <= tagged.keys()
        and all(tags.get("component") == "network" for tags in tagged.values()),
    )
    for name in config["log_groups"]:
        log = aws("logs", "describe-log-groups", "--log-group-name-prefix", name)[
            "logGroups"
        ][0]
        check(
            name + " retention and encryption",
            log["retentionInDays"] == 7 and config["log_kms_key_id"] in log["kmsKeyId"],
        )
        check(
            name + " component tag",
            aws("logs", "list-tags-log-group", "--log-group-name", name)["tags"].get(
                "component"
            )
            == "network",
        )
    check(
        "log role component tag",
        any(
            t["Key"] == "component" and t["Value"] == "network"
            for t in aws(
                "iam",
                "list-role-tags",
                "--role-name",
                config["name"] + "-vpc-flow-logs",
            )["Tags"]
        ),
    )
    check(
        "log key component tag",
        any(
            t["TagKey"] == "component" and t["TagValue"] == "network"
            for t in aws(
                "kms", "list-resource-tags", "--key-id", config["log_kms_key_id"]
            )["Tags"]
        ),
    )
    facts["resources"] = config
    facts["scope"] = (
        "AWS configuration readback after real Terraform apply; no app, packet, EKS or GCP integration claim"
    )
    Path(sys.argv[1]).write_text(json.dumps(facts, indent=2) + "\n")
    print(f"PASS: {len(facts['checks'])} AWS readback checks; evidence: {sys.argv[1]}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(
            "Run from infra/aws-foundation: python3 verify.py <evidence.json>"
        )
    verify()
