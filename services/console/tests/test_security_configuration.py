import copy
from datetime import timedelta

import pytest
from test_kubernetes_workload import NOW, Reader, registration
from test_kubernetes_workload import case as case

from bravo_console.kubernetes_workload import collect
from bravo_console.security_configuration import (
    container_checks,
    pod_checks,
    validate_configuration,
)


def facts(items):
    return {row["id"]: row["value"] for row in items}


def policy(selector, types=None):
    spec = {"podSelector": selector}
    if types is not None:
        spec["policyTypes"] = types
    return {"metadata": {"uid": "policy-one", "namespace": "target-dev"}, "spec": spec}


def test_boolean_facts_and_inheritance_do_not_leak_raw_spec():
    pod = Reader().pods[0]
    pod["spec"]["securityContext"] = {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    c = pod["spec"]["containers"][0]
    c["securityContext"] = {
        "privileged": True,
        "runAsNonRoot": False,
        "capabilities": {"drop": ["ALL"]},
    }
    found = facts(container_checks(pod, c))
    assert found["privileged"] is True and found["non_root"] is False
    assert found["seccomp"] == "RuntimeDefault" and found["drop_all_capabilities"] is True
    assert found["read_only_root"] is None
    assert facts(pod_checks(pod, []))["token_automount"] is None
    assert "must-not-leave-collector" not in str(found)


@pytest.mark.parametrize(
    "selector,count",
    [
        ({}, 1),
        ({"matchLabels": {"app": "db"}}, 0),
        ({"matchExpressions": [{"key": "app", "operator": "In", "values": ["chat"]}]}, 1),
        ({"matchExpressions": [{"key": "missing", "operator": "NotIn", "values": ["x"]}]}, 1),
        ({"matchExpressions": [{"key": "app", "operator": "Exists"}]}, 1),
        ({"matchExpressions": [{"key": "app", "operator": "DoesNotExist"}]}, 0),
    ],
)
def test_policy_selection_is_factual_not_a_deny_verdict(selector, count):
    p = Reader().pods[0]
    value = facts(pod_checks(p, [policy(selector, ["Ingress", "Egress"])]))
    assert value["ingress_policy_count"] == value["egress_policy_count"] == count
    assert "security" not in value and "isolated" not in value


def test_default_policy_types_match_kubernetes_ingress_and_nonempty_egress():
    p = Reader().pods[0]
    first = policy({})
    assert facts(pod_checks(p, [first]))["egress_policy_count"] == 0
    first["spec"]["egress"] = [{}]
    assert facts(pod_checks(p, [first]))["egress_policy_count"] == 1


def test_foreign_namespace_or_ambiguous_policy_is_unconfirmed():
    p = Reader().pods[0]
    rule = policy({})
    rule["metadata"]["namespace"] = "other"
    with pytest.raises(ValueError):
        pod_checks(p, [rule])
    with pytest.raises(ValueError):
        pod_checks(
            p,
            [policy({"matchExpressions": [{"key": "app", "operator": "Exists", "values": ["x"]}]})],
        )


def test_configurations_expire_with_environment_and_fail_closed_on_uid_change(case):
    reader, reg, source, clock = case
    before = source.read()
    assert len(before["security_config"]) == 2
    assert all(n["security"] == "unknown" for n in before["graph"]["nodes"])
    clock[0] += timedelta(seconds=91)
    assert source.read()["security_config"] == {}
    clock[0] = NOW
    source.collect = lambda r: copy.deepcopy(collect(reader, r, source_revision="a" * 40, now=NOW))
    original = source.collect

    def wrong(r):
        value = original(r)
        next(iter(value["security_config"].values()))["resource_uid"] = "replaced"
        return value

    source.collect = wrong
    source.refresh(reg)
    assert source.read()["security_config"] == {}


@pytest.mark.parametrize("value", [True, -1, 257, "1"])
def test_policy_counts_are_bounded_and_not_boolean(value):
    nodes = [{"id": "pod", "resource_uid": "uid"}]
    with pytest.raises(ValueError):
        validate_configuration(
            {
                "pod": {
                    "resource_uid": "uid",
                    "checks": [{"id": "ingress_policy_count", "value": value}],
                }
            },
            nodes,
        )


def test_only_allowlisted_facts_leave_collector():
    value = collect(Reader(), registration(), source_revision="a" * 40, now=NOW)
    for fact in value["security_config"].values():
        assert set(fact) == {"resource_uid", "checks"}
        assert all(set(row) == {"id", "value"} for row in fact["checks"])


@pytest.mark.parametrize("change", ["pod_uid", "pod_settings", "policy"])
def test_configuration_changed_during_probe_is_not_published(change):
    reader = Reader()
    old_get = reader.get
    rules = [policy({})]

    def get(kind, name=None, **kwargs):
        if kind == "networkpolicies":
            return {"items": copy.deepcopy(rules)}
        return old_get(kind, name, **kwargs)

    reader.get = get

    def probe(*_):
        if change == "pod_uid":
            reader.pods[0]["metadata"]["uid"] = "replacement"
        if change == "pod_settings":
            reader.pods[0]["spec"]["hostNetwork"] = True
        if change == "policy":
            rules.clear()
        return True

    reader.health = probe
    with pytest.raises(Exception, match="SECURITY_CONFIGURATION_CHANGED_DURING_READ"):
        collect(reader, registration(), source_revision="a" * 40, now=NOW)
