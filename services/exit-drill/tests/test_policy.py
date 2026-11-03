"""차단 정책 생성기. 무엇을 막고 남기는지가 입력에서 결정되고, 위험한 입력은 만들지 않고 실패한다."""

import json
import random

import pytest
from helpers import (
    TAILNET,
    healthy_pods,
    inventory_doc,
    network_policy,
    pod,
    redigest,
    snapshot_doc,
    spec_doc,
)

from bravo_exit_drill.cluster import parse_snapshot
from bravo_exit_drill.inventory_input import parse_inventory
from bravo_exit_drill.policy import PlanRefused, build_plan, plan_is_intact
from bravo_exit_drill.spec import SpecError, parse_spec


def plan_for(spec=None, inventory=None, snapshot=None) -> dict:
    return build_plan(
        parse_spec(spec_doc() if spec is None else spec),
        parse_inventory(inventory_doc() if inventory is None else inventory, candidate="onprem-demo"),
        parse_snapshot(snapshot_doc() if snapshot is None else snapshot, namespace="target-dev"),
    )


def refused_codes(**kwargs) -> set[str]:
    with pytest.raises(PlanRefused) as caught:
        plan_for(**kwargs)
    return {v.code for v in caught.value.violations}


def route(spec: dict, route_id: str) -> dict:
    return next(r for r in spec["routes"] if r["id"] == route_id)


def decisions(plan: dict) -> dict:
    return {d["route_id"]: d for d in plan["decisions"]}


def manifest(plan: dict, name: str) -> dict:
    return next(m for m in plan["manifests"] if m["metadata"]["name"] == name)


def test_decisions_come_from_input():
    got = decisions(plan_for())
    assert {k: v["action"] for k, v in got.items()} == {
        "app-to-objects": "ALLOW",
        "app-to-pooler": "ALLOW",
        "aws-iam": "BLOCK",
        "aws-kms": "BLOCK",
        "aws-s3": "BLOCK",
        "aws-secretsmanager": "BLOCK",
        "aws-sts": "BLOCK",
        "dns": "ALLOW",
        "pooler-to-db": "ALLOW",
        "pooler-to-source": "BLOCK",
        "replication-forward": "KEEP",
        "replication-reverse": "KEEP",
    }


def test_aws_routes_are_derived_from_inventory_for_the_aws_client_only():
    got = decisions(plan_for())
    assert got["aws-s3"]["probe"] == {"kind": "tcp", "host": "s3.ap-northeast-2.amazonaws.com", "port": 443}
    assert got["aws-iam"]["probe"]["host"] == "iam.amazonaws.com"
    assert got["aws-s3"]["owners"] == ["app"]
    assert got["aws-s3"]["roles"] == ["object_store"]


def test_same_input_gives_same_bytes_and_order_does_not_matter():
    first = json.dumps(plan_for(), sort_keys=True, ensure_ascii=False)
    spec = spec_doc()
    random.Random(7).shuffle(spec["routes"])
    spec["workloads"] = dict(reversed(list(spec["workloads"].items())))
    pods = healthy_pods()
    random.Random(7).shuffle(pods)
    second = json.dumps(plan_for(spec=spec, snapshot=snapshot_doc(pods=pods)), sort_keys=True, ensure_ascii=False)
    assert first == second


def test_changed_input_changes_digest():
    other = spec_doc()
    route(other, "app-to-objects")["ports"] = [{"protocol": "TCP", "port": 8334}]
    assert plan_for()["digest"] != plan_for(spec=other)["digest"]


@pytest.mark.parametrize("field", [
    "namespace", "candidate", "inventory_digest", "workloads", "decisions", "manifests",
    "new_connection_checks", "unmapped_dependencies", "declared_residuals",
])
def test_editing_any_part_of_the_plan_breaks_it(field):
    """계획의 어느 칸이든 손대면 쓸 수 없어야 한다. 빠진 칸이 있으면 그 칸은 몰래 바꿀 수 있다."""
    plan = plan_for()
    assert plan_is_intact(plan)
    assert not plan_is_intact({**plan, field: "edited"})


def test_restarted_pods_do_not_change_digest():
    """재시작 뒤 Pod UID가 바뀌어도 같은 차단 계획이다."""
    pods = healthy_pods()
    pods[0]["metadata"]["uid"] = "uid-after-restart"
    assert plan_for()["digest"] == plan_for(snapshot=snapshot_doc(pods=pods))["digest"]


def test_manifests_select_only_app_role_workloads():
    plan = plan_for()
    assert sorted(m["metadata"]["name"] for m in plan["manifests"]) == [
        "exit-drill-egress-app",
        "exit-drill-egress-pooler",
    ]
    for item in plan["manifests"]:
        assert item["kind"] == "NetworkPolicy"
        assert item["metadata"]["namespace"] == "target-dev"
        assert item["metadata"]["labels"]["component"] == "target"
        assert item["metadata"]["annotations"]["bravo.exit-drill/plan-digest"] == plan["digest"]
        assert "cnpg.io/cluster" not in item["spec"]["podSelector"]["matchLabels"]


def test_manifests_change_egress_only_so_ingress_replication_stays():
    for item in plan_for()["manifests"]:
        assert item["spec"]["policyTypes"] == ["Egress"]
        assert "ingress" not in item["spec"]


def test_manifests_allow_only_declared_local_peers():
    plan = plan_for()
    assert manifest(plan, "exit-drill-egress-app")["spec"]["egress"] == [
        {
            "to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "target-seaweedfs"}}}],
            "ports": [{"protocol": "TCP", "port": 8333}],
        },
        {
            "to": [{"podSelector": {"matchLabels": {"app": "target-pgbouncer"}}}],
            "ports": [{"protocol": "TCP", "port": 5432}],
        },
        {
            "to": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }],
            "ports": [{"protocol": "TCP", "port": 53}, {"protocol": "UDP", "port": 53}],
        },
    ]
    assert manifest(plan, "exit-drill-egress-pooler")["spec"]["egress"][1]["to"] == [
        {"podSelector": {"matchLabels": {"cnpg.io/cluster": "target-db", "cnpg.io/instanceRole": "primary"}}}
    ]


def test_manifests_never_mention_a_way_out():
    rendered = json.dumps(plan_for()["manifests"])
    assert "ipBlock" not in rendered
    assert TAILNET not in rendered
    assert "amazonaws" not in rendered


# ---- 복제 경로를 실수로 막으면 실패한다 ----


def test_selector_shared_with_db_pods_is_refused():
    """공통 레이블로 앱을 고르면 DB Pod까지 잡혀 복제 송신이 막힌다."""
    spec = spec_doc()
    spec["workloads"]["app"]["selector"] = {"component": "target"}
    pods = [
        pod("demo-chat-1", {"app": "demo-chat", "component": "target"}),
        pod("pgbouncer-1", {"app": "target-pgbouncer"}),
        pod("target-db-1", {"cnpg.io/cluster": "target-db", "component": "target"}),
        pod("control-1", {"bravo-exit-drill-control": "true"}),
    ]
    assert "REPLICATION_OWNER_SELECTED" in refused_codes(spec=spec, snapshot=snapshot_doc(pods=pods))


def test_block_declared_on_the_replication_path_is_refused():
    spec = spec_doc()
    route(spec, "pooler-to-source")["to"] = dict(route(spec, "replication-forward")["to"])
    assert "BLOCK_HITS_REPLICATION" in refused_codes(spec=spec)


def test_block_probing_the_replication_address_is_refused():
    spec = spec_doc()
    route(spec, "pooler-to-source")["probe"] = dict(route(spec, "replication-forward")["probe"])
    assert "BLOCK_HITS_REPLICATION" in refused_codes(spec=spec)


def test_replication_owned_by_app_is_refused():
    spec = spec_doc()
    route(spec, "replication-forward")["owners"] = ["app"]
    assert "OWNER_ROLE_MISMATCH" in refused_codes(spec=spec)


def test_block_owned_by_db_is_refused():
    spec = spec_doc()
    route(spec, "pooler-to-source")["owners"] = ["db"]
    assert "OWNER_ROLE_MISMATCH" in refused_codes(spec=spec)


def test_missing_replication_route_is_refused():
    spec = spec_doc()
    spec["routes"] = [r for r in spec["routes"] if r["class"] != "replication"]
    assert "NO_REPLICATION_ROUTE" in refused_codes(spec=spec)


def test_existing_egress_policy_already_blocking_replication_is_refused():
    deny = network_policy("db-egress", {"matchLabels": {"cnpg.io/cluster": "target-db"}}, egress=[])
    assert "REPLICATION_BLOCKED" in refused_codes(snapshot=snapshot_doc(policies=[deny]))


def test_egress_policy_allowing_the_outgoing_channel_passes():
    allow = network_policy("db-egress", {"matchLabels": {"cnpg.io/cluster": "target-db"}}, egress=[{
        "to": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": TAILNET}},
            "podSelector": {"matchExpressions": [{
                "key": "tailscale.com/parent-resource",
                "operator": "In",
                "values": ["bravo-target-replication"],
            }]},
        }],
    }])
    assert plan_for(snapshot=snapshot_doc(policies=[allow]))["manifests"]


def test_egress_policy_missing_the_outgoing_channel_is_refused():
    other = network_policy("db-egress", {"matchLabels": {"cnpg.io/cluster": "target-db"}}, egress=[{
        "to": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": TAILNET}},
            "podSelector": {"matchLabels": {"tailscale.com/parent-resource": "target-db-tailnet"}},
        }],
    }])
    assert "REPLICATION_BLOCKED" in refused_codes(snapshot=snapshot_doc(policies=[other]))


def test_incoming_channel_is_not_judged_by_egress():
    """역방향은 출발지가 접속해 온다. DB의 송신 정책으로 막히지 않으니 거부 사유가 아니다."""
    with pytest.raises(PlanRefused) as caught:
        plan_for(snapshot=snapshot_doc(policies=[
            network_policy("db-egress", {"matchLabels": {"cnpg.io/cluster": "target-db"}}, egress=[]),
        ]))
    assert {v.subject for v in caught.value.violations} == {"replication-forward:target-db-1"}


def test_ingress_policy_shutting_out_the_incoming_channel_is_refused():
    deny = network_policy("db-ingress", {"matchLabels": {"cnpg.io/cluster": "target-db"}}, types=("Ingress",), ingress=[])
    with pytest.raises(PlanRefused) as caught:
        plan_for(snapshot=snapshot_doc(policies=[deny]))
    assert {(v.code, v.subject) for v in caught.value.violations} == {("REPLICATION_BLOCKED", "replication-reverse:target-db-1")}


def test_ingress_policy_admitting_the_incoming_channel_passes():
    allow = network_policy("db-ingress", {"matchLabels": {"cnpg.io/cluster": "target-db"}}, types=("Ingress",), ingress=[{
        "from": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": TAILNET}},
            "podSelector": {"matchLabels": {"tailscale.com/parent-resource": "target-db-tailnet"}},
        }],
    }])
    assert plan_for(snapshot=snapshot_doc(policies=[allow]))["manifests"]


def test_policy_without_types_always_limits_ingress():
    """policyTypes를 생략하면 쿠버네티스는 수신 정책으로 본다."""
    implicit = network_policy("implicit", {"matchLabels": {"cnpg.io/cluster": "target-db"}})
    del implicit["spec"]["policyTypes"]
    assert "REPLICATION_BLOCKED" in refused_codes(snapshot=snapshot_doc(policies=[implicit]))


def test_replication_must_stay_postgres_over_tailnet():
    spec = spec_doc()
    route(spec, "replication-forward")["ports"] = [{"protocol": "TCP", "port": 443}]
    assert "REPLICATION_NOT_POSTGRES" in refused_codes(spec=spec)
    spec = spec_doc()
    route(spec, "replication-forward")["to"] = {"host": "s3.ap-northeast-2.amazonaws.com"}
    assert "REPLICATION_NOT_TAILNET" in refused_codes(spec=spec)


# ---- AWS 경로가 남으면 실패한다 ----


@pytest.mark.parametrize(
    ("label", "egress"),
    [
        ("모든 곳 허용", [{}]),
        ("to 없는 포트 허용", [{"ports": [{"protocol": "TCP", "port": 443}]}]),
        ("외부 대역", [{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}]),
        ("사설 대역도 프록시를 포함할 수 있다", [{"to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}]}]),
        ("tailnet 프록시 전체", [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": TAILNET}}}]}]),
        ("모든 네임스페이스", [{"to": [{"namespaceSelector": {}}]}]),
        ("같은 네임스페이스 전체", [{"to": [{"podSelector": {}}]}]),
        (
            "출발지 풀러 프록시",
            [{"to": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": TAILNET}},
                "podSelector": {"matchLabels": {"tailscale.com/parent-resource": "bravo-target-pooler"}},
            }]}],
        ),
    ],
)
def test_additive_existing_policy_leaving_a_way_out_is_refused(label, egress):
    """NetworkPolicy는 합집합이다. 새 정책을 더해도 기존 허용이 남으면 차단되지 않는다."""
    leak = network_policy("old-allow", {"matchLabels": {"app": "demo-chat"}}, egress=egress)
    assert "UNDECLARED_EGRESS" in refused_codes(snapshot=snapshot_doc(policies=[leak])), label


def test_existing_policy_selecting_all_pods_counts():
    leak = network_policy("open-all", {}, egress=[{}])
    assert "UNDECLARED_EGRESS" in refused_codes(snapshot=snapshot_doc(policies=[leak]))


def test_existing_policy_selected_by_expression_counts():
    leak = network_policy(
        "by-expression",
        {"matchExpressions": [{"key": "app", "operator": "In", "values": ["demo-chat"]}]},
        egress=[{"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}],
    )
    assert "UNDECLARED_EGRESS" in refused_codes(snapshot=snapshot_doc(policies=[leak]))


def test_policy_without_types_but_with_egress_counts():
    leak = network_policy("implicit-types", {"matchLabels": {"app": "demo-chat"}}, egress=[{}])
    del leak["spec"]["policyTypes"]
    assert "UNDECLARED_EGRESS" in refused_codes(snapshot=snapshot_doc(policies=[leak]))


@pytest.mark.parametrize(
    "policy",
    [
        network_policy("other-pods", {"matchLabels": {"app": "something-else"}}, egress=[{}]),
        network_policy("ingress-only", {"matchLabels": {"app": "demo-chat"}}, types=("Ingress",)),
        network_policy("default-deny", {}, egress=[]),
        network_policy(
            "same-as-plan",
            {"matchLabels": {"app": "demo-chat"}},
            egress=[{
                "to": [{"podSelector": {"matchLabels": {"app": "target-pgbouncer"}}}],
                "ports": [{"protocol": "TCP", "port": 5432}],
            }],
        ),
    ],
)
def test_existing_policies_that_add_nothing_do_not_refuse(policy):
    # default-deny는 DB도 고르므로 복제 허용 정책을 함께 둔다. 그렇지 않으면 복제가 막혀 있다.
    db_allow = network_policy("db-egress", {"matchLabels": {"cnpg.io/cluster": "target-db"}}, egress=[{}])
    assert plan_for(snapshot=snapshot_doc(policies=[policy, db_allow]))["manifests"]


def test_wider_port_on_a_declared_peer_is_refused():
    leak = network_policy("wide-port", {"matchLabels": {"app": "demo-chat"}}, egress=[{
        "to": [{"podSelector": {"matchLabels": {"app": "target-pgbouncer"}}}],
    }])
    assert "UNDECLARED_EGRESS" in refused_codes(snapshot=snapshot_doc(policies=[leak]))


def test_host_network_pod_is_refused():
    """hostNetwork Pod에는 NetworkPolicy가 걸리지 않아 AWS 경로가 그대로 남는다."""
    pods = healthy_pods()
    pods[0] = pod("demo-chat-1", {"app": "demo-chat"}, host_network=True)
    assert "HOST_NETWORK_POD" in refused_codes(snapshot=snapshot_doc(pods=pods))


def test_unclassified_running_pod_is_refused():
    """명세에 없는 Pod는 차단 범위 밖이라 AWS에 그대로 닿을 수 있다."""
    pods = [*healthy_pods(), pod("forgotten-worker", {"app": "worker"})]
    assert "UNCLASSIFIED_POD" in refused_codes(snapshot=snapshot_doc(pods=pods))


def test_finished_job_pod_is_ignored():
    pods = [*healthy_pods(), pod("migration-job", {"job": "schema"}, phase="Succeeded")]
    assert plan_for(snapshot=snapshot_doc(pods=pods))["manifests"]


@pytest.mark.parametrize("to", [
    {"host": "s3.ap-northeast-2.amazonaws.com"},
    {"namespace": TAILNET, "selector": {"tailscale.com/parent-resource": "bravo-target-pooler"}},
    {"selector": {"tailscale.com/parent-resource": "bravo-target-pooler"}},
])
def test_local_route_that_leaves_the_cluster_is_refused(to):
    spec = spec_doc()
    route(spec, "app-to-objects")["to"] = to
    assert "LOCAL_ROUTE_LEAVES_CLUSTER" in refused_codes(spec=spec)


def _gateway_routes(local_selector: dict, blocked_selector: dict) -> dict:
    spec = spec_doc()
    spec["routes"].append({
        "id": "app-to-gateway", "class": "local", "owners": ["app"], "to": {"selector": local_selector},
        "ports": [{"protocol": "TCP", "port": 9000}], "probe": {"kind": "tcp", "host": "gateway.target-dev.svc", "port": 9000},
    })
    spec["routes"].append({
        "id": "app-to-source-gateway", "class": "source_access", "owners": ["app"], "to": {"selector": blocked_selector},
        "ports": [{"protocol": "TCP", "port": 9000}], "probe": {"kind": "tcp", "host": "source-gateway.target-dev.svc", "port": 9000},
    })
    return spec


@pytest.mark.parametrize(("local", "blocked"), [
    ({"app": "gateway"}, {"app": "gateway"}),
    ({"app": "gateway"}, {"app": "gateway", "tier": "edge"}),
    ({"app": "gateway", "tier": "edge"}, {"app": "gateway"}),
])
def test_destination_both_allowed_and_blocked_is_refused(local, blocked):
    """허용 목록에 있으면 BLOCK이라고 적어도 막히지 않는다."""
    assert "BLOCK_ALSO_ALLOWED" in refused_codes(spec=_gateway_routes(local, blocked))


def test_different_destinations_are_not_a_conflict():
    plan = plan_for(spec=_gateway_routes({"app": "gateway"}, {"app": "source-gateway"}))
    assert decisions(plan)["app-to-source-gateway"]["action"] == "BLOCK"


def test_same_destination_for_different_owners_is_not_a_conflict():
    spec = _gateway_routes({"app": "gateway"}, {"app": "gateway"})
    route(spec, "app-to-source-gateway")["owners"] = ["pooler"]
    assert decisions(plan_for(spec=spec))["app-to-gateway"]["action"] == "ALLOW"


def test_open_local_selector_is_refused():
    spec = spec_doc()
    route(spec, "app-to-objects")["to"] = {"selector": {}}
    assert "LOCAL_ROUTE_LEAVES_CLUSTER" in refused_codes(spec=spec)


def test_no_source_route_means_nothing_to_block():
    spec = spec_doc()
    spec["routes"] = [r for r in spec["routes"] if r["class"] != "source_access"]
    doc = inventory_doc()
    doc["source"]["env_kind"] = "gcp"
    assert "NO_SOURCE_ROUTE" in refused_codes(spec=spec, inventory=redigest(doc))


def test_aws_dependency_without_an_aws_client_is_refused():
    """AWS를 쓴다고 나왔는데 누구에게서 잴지 모르면 AWS 경로를 확인할 수 없다."""
    spec = spec_doc()
    del spec["workloads"]["app"]["aws_client"]
    assert "NO_AWS_CLIENT" in refused_codes(spec=spec)


def test_selector_that_matches_nothing_is_refused():
    """오타 난 선택자의 정책은 조용히 아무것도 막지 않는다."""
    spec = spec_doc()
    spec["workloads"]["app"]["selector"] = {"app": "demo-chatt"}
    pods = [p for p in healthy_pods() if p["metadata"]["name"] != "demo-chat-1"]
    assert "SELECTOR_MATCHES_NOTHING" in refused_codes(spec=spec, snapshot=snapshot_doc(pods=pods))


def test_empty_selector_is_refused():
    spec = spec_doc()
    spec["workloads"]["app"]["selector"] = {}
    assert "EMPTY_SELECTOR" in refused_codes(spec=spec)


def test_control_selected_by_policy_is_refused():
    """대조군이 차단되면 대상이 살아 있는지 잴 수 없다."""
    spec = spec_doc()
    spec["workloads"]["control"]["selector"] = {"app": "demo-chat"}
    assert "CONTROL_SELECTED" in refused_codes(spec=spec)


def test_missing_control_is_refused():
    spec = spec_doc()
    del spec["workloads"]["control"]
    pods = [p for p in healthy_pods() if p["metadata"]["name"] != "control-1"]
    assert "NO_CONTROL" in refused_codes(spec=spec, snapshot=snapshot_doc(pods=pods))


def test_unknown_owner_is_refused():
    spec = spec_doc()
    route(spec, "dns")["owners"] = ["nobody"]
    assert "UNKNOWN_WORKLOAD" in refused_codes(spec=spec)


def test_duplicate_route_id_is_refused():
    spec = spec_doc()
    spec["routes"].append(dict(route(spec, "dns")))
    assert "DUPLICATE_ROUTE" in refused_codes(spec=spec)


def test_route_colliding_with_derived_aws_route_is_refused():
    spec = spec_doc()
    spec["routes"].append({
        "id": "aws-s3", "class": "local", "owners": ["app"], "to": {"selector": {"app": "x"}},
        "ports": [{"protocol": "TCP", "port": 1}], "probe": {"kind": "tcp", "host": "x", "port": 1},
    })
    assert "DUPLICATE_ROUTE" in refused_codes(spec=spec)


def test_conflicting_policy_with_our_name_is_refused():
    other = network_policy("exit-drill-egress-app", {"matchLabels": {"app": "demo-chat"}}, egress=[])
    assert "POLICY_NAME_CONFLICT" in refused_codes(snapshot=snapshot_doc(policies=[other]))


def test_already_applied_identical_plan_can_be_rendered_again():
    plan = plan_for()
    assert plan_for(snapshot=snapshot_doc(policies=plan["manifests"]))["digest"] == plan["digest"]


def test_all_violations_are_reported_together():
    spec = spec_doc()
    route(spec, "pooler-to-source")["owners"] = ["db"]
    route(spec, "replication-forward")["ports"] = [{"protocol": "TCP", "port": 443}]
    assert {"OWNER_ROLE_MISMATCH", "REPLICATION_NOT_POSTGRES"} <= refused_codes(spec=spec)


def test_dependency_without_route_is_listed_not_dropped():
    doc = inventory_doc()
    doc["items"].append({
        **doc["items"][0], "id": "item-006", "role": "unknown", "service": "google.cloud",
        "grade": "unknown", "agent_needed": True,
    })
    plan = plan_for(inventory=redigest(doc))
    assert plan["unmapped_dependencies"] == [{"id": "item-006", "role": "unknown", "services": ["google.cloud"]}]


def test_role_on_a_kept_route_does_not_count_as_measured():
    """복제 경로는 차단을 재지 않는다. 거기에 역할을 달아도 그 의존성을 잰 것이 아니다."""
    spec = spec_doc()
    del route(spec, "pooler-to-source")["roles"]
    route(spec, "replication-forward")["roles"] = ["db"]
    assert [d["id"] for d in plan_for(spec=spec)["unmapped_dependencies"]] == ["item-001"]


def test_plan_has_no_unmapped_for_real_inventory():
    assert plan_for()["unmapped_dependencies"] == []


def test_plan_binds_inventory_and_snapshot():
    plan = plan_for()
    assert plan["inventory_digest"] == inventory_doc()["digest"]
    assert plan["candidate"] == "onprem-demo"
    assert plan["snapshot_digest"].startswith("sha256:")


# ---- 입력 형식 ----


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(schema_version="v9"),
        lambda s: s.update(extra_field=1),
        lambda s: s.pop("namespace"),
        lambda s: s["routes"][0].update({"class": "nonsense"}),
        lambda s: s["routes"][0].update(unknown=1),
        lambda s: s["routes"][0].update(to={"host": "x", "selector": {"a": "b"}}),
        lambda s: s["routes"][0].update(to={}),
        lambda s: s["routes"][0].update(ports=[]),
        lambda s: s["routes"][0].update(ports=[{"protocol": "TCP", "port": 0}]),
        lambda s: s["routes"][0].update(ports=[{"protocol": "ICMP", "port": 1}]),
        lambda s: s["routes"][0].pop("probe"),
        lambda s: s["routes"][0].update(probe={"kind": "tcp", "host": "x"}),
        lambda s: s["workloads"]["app"].update(role="admin"),
        lambda s: s.update(new_connection_checks=[]),
        lambda s: s["new_connection_checks"][0].update(kind="ftp"),
        lambda s: s["routes"][5].pop("evidence"),
        lambda s: s["routes"][6].update(evidence="hope"),
        lambda s: s["routes"][6].update(direction="sideways"),
        lambda s: s["routes"][6].update(evidence="tcp", probe={"kind": "tcp", "host": "x", "port": 5432}),
        lambda s: s["routes"][0].update(direction="ingress"),
        lambda s: s["routes"][0].update({"class": ["local"]}),
        lambda s: s["routes"][0].update(roles=[1]),
        lambda s: s["routes"][0].update(probe={"kind": ["tcp"], "host": "x", "port": 1}),
        lambda s: s["workloads"]["app"].update(role=["app"]),
        lambda s: s["new_connection_checks"][0].update(kind=["http"]),
        lambda s: s["new_connection_checks"][0].update(id=1),
        lambda s: s["new_connection_checks"].append(dict(s["new_connection_checks"][0])),
    ],
)
def test_malformed_spec_stops(mutate):
    spec = spec_doc()
    mutate(spec)
    with pytest.raises(SpecError):
        parse_spec(spec)


def test_snapshot_of_another_namespace_stops():
    from bravo_exit_drill.cluster import SnapshotError

    with pytest.raises(SnapshotError):
        parse_snapshot(snapshot_doc(pods=[pod("x", {}, namespace="source-dev")]), namespace="target-dev")


@pytest.mark.parametrize("bad_item", [
    {"kind": "Pod", "metadata": {"namespace": "target-dev", "name": "x", "labels": "ab"}},
    {"kind": "NetworkPolicy", "metadata": {"namespace": "target-dev", "name": "x"}, "spec": {"ingress": [{"from": [{"podSelector": {"matchExpressions": [{"key": "a", "operator": "Near"}]}}]}]}},
])
def test_malformed_snapshot_item_stops(bad_item):
    from bravo_exit_drill.cluster import SnapshotError

    with pytest.raises(SnapshotError):
        parse_snapshot({"items": [bad_item]}, namespace="target-dev")


def test_snapshot_with_unknown_selector_operator_stops():
    from bravo_exit_drill.cluster import SnapshotError

    bad = network_policy("bad", {"matchExpressions": [{"key": "a", "operator": "Near"}]}, egress=[])
    with pytest.raises(SnapshotError):
        plan_for(snapshot=snapshot_doc(policies=[bad]))
