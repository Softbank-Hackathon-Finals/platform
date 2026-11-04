"""NetworkPolicy 해석. 넓게 잘못 읽어도 좁게 잘못 읽어도 거짓 판정이 나오므로 양쪽을 모두 잰다."""

import pytest
from helpers import TAILNET, inventory_doc, network_policy, snapshot_doc, spec_doc

from bravo_exit_drill.cluster import parse_snapshot, path_open, selector_matches
from bravo_exit_drill.inventory_input import parse_inventory
from bravo_exit_drill.policy import PlanRefused, build_plan
from bravo_exit_drill.spec import parse_spec

PROXY = {"tailscale.com/parent-resource": "bravo-target-replication"}
DB = {"cnpg.io/cluster": "target-db"}


@pytest.mark.parametrize(
    ("selector", "labels", "expected"),
    [
        ({}, {"a": "1"}, True),
        ({"matchLabels": {"a": "1"}}, {"a": "1", "b": "2"}, True),
        ({"matchLabels": {"a": "1"}}, {"a": "2"}, False),
        ({"matchLabels": {"a": "1"}}, {}, False),
        ({"matchExpressions": [{"key": "a", "operator": "In", "values": ["1", "2"]}]}, {"a": "2"}, True),
        ({"matchExpressions": [{"key": "a", "operator": "In", "values": ["1"]}]}, {"a": "2"}, False),
        ({"matchExpressions": [{"key": "a", "operator": "In", "values": ["1"]}]}, {}, False),
        ({"matchExpressions": [{"key": "a", "operator": "NotIn", "values": ["1"]}]}, {"a": "2"}, True),
        ({"matchExpressions": [{"key": "a", "operator": "NotIn", "values": ["1"]}]}, {}, True),
        ({"matchExpressions": [{"key": "a", "operator": "NotIn", "values": ["1"]}]}, {"a": "1"}, False),
        ({"matchExpressions": [{"key": "a", "operator": "Exists"}]}, {"a": "x"}, True),
        ({"matchExpressions": [{"key": "a", "operator": "Exists"}]}, {}, False),
        ({"matchExpressions": [{"key": "a", "operator": "DoesNotExist"}]}, {}, True),
        ({"matchExpressions": [{"key": "a", "operator": "DoesNotExist"}]}, {"a": "x"}, False),
        ({"matchLabels": {"a": "1"}, "matchExpressions": [{"key": "b", "operator": "Exists"}]}, {"a": "1"}, False),
    ],
)
def test_selector_matches(selector, labels, expected):
    assert selector_matches(selector, labels) is expected


def policy(egress):
    return network_policy("db-egress", {"matchLabels": DB}, egress=egress)


POSTGRES = [("TCP", 5432)]


def allows(*policies) -> bool:
    return path_open(policies, DB, "target-dev", TAILNET, PROXY, "egress", POSTGRES)


def admits(*rules) -> bool:
    policy_doc = network_policy("in", {"matchLabels": DB}, types=("Ingress",), ingress=list(rules))
    return path_open([policy_doc], DB, "target-dev", TAILNET, PROXY, "ingress", POSTGRES)


def tailnet_peer(**extra):
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": TAILNET}}, **extra}


def test_no_egress_policy_means_allowed():
    assert allows()


def test_policy_for_other_pods_does_not_limit():
    assert allows(network_policy("other", {"matchLabels": {"app": "x"}}, egress=[]))


def test_ingress_only_policy_does_not_limit():
    assert allows(network_policy("in", {"matchLabels": DB}, types=("Ingress",)))


def test_empty_egress_blocks():
    assert not allows(policy([]))


def test_rule_without_to_allows_all():
    assert allows(policy([{}]))


def test_matching_peer_allows():
    assert allows(policy([{"to": [tailnet_peer(podSelector={"matchLabels": PROXY})]}]))


def test_namespace_only_peer_allows():
    assert allows(policy([{"to": [tailnet_peer()]}]))


def test_other_proxy_does_not_allow():
    other = {"tailscale.com/parent-resource": "bravo-target-pooler"}
    assert not allows(policy([{"to": [tailnet_peer(podSelector={"matchLabels": other})]}]))


def test_same_labels_in_own_namespace_is_not_the_proxy():
    """namespaceSelector가 없으면 자기 네임스페이스만 가리킨다."""
    assert not allows(policy([{"to": [{"podSelector": {"matchLabels": PROXY}}]}]))


def test_peer_in_another_namespace_does_not_allow():
    other = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}
    assert not allows(policy([{"to": [other]}]))


def test_ip_block_cannot_prove_the_path():
    """프록시 Pod 주소를 모르므로 ipBlock으로는 복제 경로가 열렸다고 말할 수 없다."""
    assert not allows(policy([{"to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}]}]))


def test_ip_block_cannot_prove_a_path_in_the_same_namespace_either():
    wide = policy([{"to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}]}])
    assert not path_open([wide], DB, "target-dev", "target-dev", PROXY, "egress", POSTGRES)


def test_any_of_several_policies_is_enough():
    """NetworkPolicy는 합집합이다."""
    assert allows(policy([]), network_policy("second", {"matchLabels": DB}, egress=[{"to": [tailnet_peer()]}]))


def test_egress_policy_does_not_limit_ingress():
    assert path_open([policy([])], DB, "target-dev", TAILNET, PROXY, "ingress", POSTGRES)


def test_ingress_rule_uses_from_peers():
    rule = {"from": [tailnet_peer(podSelector={"matchLabels": PROXY})]}
    assert admits(rule)
    other = network_policy("in", {"matchLabels": DB}, types=("Ingress",), ingress=[rule])
    assert not path_open([other], DB, "target-dev", TAILNET, {"tailscale.com/parent-resource": "someone-else"}, "ingress", POSTGRES)


def test_ingress_rule_on_another_port_does_not_admit():
    """수신은 DB의 실제 포트로 들어온다. 다른 포트만 열면 복제는 막혀 있다."""
    assert not admits({"from": [tailnet_peer()], "ports": [{"protocol": "TCP", "port": 9999}]})


def test_ingress_rule_without_peer_on_another_port_does_not_admit():
    assert not admits({"ports": [{"protocol": "TCP", "port": 80}]})


def test_ingress_rule_on_the_right_port_admits():
    assert admits({"from": [tailnet_peer()], "ports": [{"protocol": "TCP", "port": 5432}]})


def test_ingress_rule_with_default_protocol_admits():
    assert admits({"from": [tailnet_peer()], "ports": [{"port": 5432}]})


def test_ingress_rule_with_other_protocol_does_not_admit():
    assert not admits({"from": [tailnet_peer()], "ports": [{"protocol": "UDP", "port": 5432}]})


def test_ingress_rule_with_named_port_cannot_be_proven():
    assert not admits({"from": [tailnet_peer()], "ports": [{"protocol": "TCP", "port": "postgres"}]})


def test_ingress_rule_with_port_range_admits():
    assert admits({"from": [tailnet_peer()], "ports": [{"protocol": "TCP", "port": 5000, "endPort": 6000}]})


def test_egress_port_is_not_judged_because_proxy_ports_are_allocated():
    assert allows(policy([{"to": [tailnet_peer()], "ports": [{"protocol": "TCP", "port": 10042}]}]))


@pytest.mark.parametrize("selector", [
    {"matchExpressions": [{"key": "team", "operator": "DoesNotExist"}]},
    {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "NotIn", "values": ["kube-system"]}]},
    {"matchLabels": {"team": "net"}},
])
def test_namespace_selector_that_does_not_name_the_namespace_cannot_be_proven(selector):
    """네임스페이스의 다른 레이블을 모르니 이런 선택자로는 열렸다고 말할 수 없다."""
    assert not allows(policy([{"to": [{"namespaceSelector": selector}]}]))


@pytest.mark.parametrize("selector", [
    {},
    {"matchLabels": {"kubernetes.io/metadata.name": TAILNET}},
    {"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "In", "values": [TAILNET, "x"]}]},
])
def test_namespace_selector_naming_the_namespace_allows(selector):
    assert allows(policy([{"to": [{"namespaceSelector": selector}]}]))


def test_missing_types_with_empty_egress_does_not_limit_egress():
    """policyTypes를 생략하면 egress 규칙이 있을 때만 송신을 제한한다. 빈 목록은 규칙이 없는 것이다."""
    implicit = policy([])
    del implicit["spec"]["policyTypes"]
    assert allows(implicit)


def test_expression_selecting_other_pods_does_not_count_as_leak():
    """표현식을 무시하면 전부 고른 것으로 잘못 읽어 멀쩡한 환경을 거부한다."""
    other = network_policy(
        "others",
        {"matchExpressions": [{"key": "app", "operator": "In", "values": ["something-else"]}]},
        egress=[{}],
    )
    plan = build_plan(
        parse_spec(spec_doc()),
        parse_inventory(inventory_doc(), candidate="onprem-demo"),
        parse_snapshot(snapshot_doc(policies=[other]), namespace="target-dev"),
    )
    assert plan["manifests"]


def test_replication_kept_only_through_own_namespace_label_is_refused():
    wrong = network_policy("db-egress", {"matchLabels": DB}, egress=[{"to": [{"podSelector": {"matchLabels": PROXY}}]}])
    with pytest.raises(PlanRefused) as caught:
        build_plan(
            parse_spec(spec_doc()),
            parse_inventory(inventory_doc(), candidate="onprem-demo"),
            parse_snapshot(snapshot_doc(policies=[wrong]), namespace="target-dev"),
        )
    assert "REPLICATION_BLOCKED" in {v.code for v in caught.value.violations}
