"""T44 operator-access: people reach a cluster API through its Tailscale API server proxy (auth mode)."""

import copy
import json
from pathlib import Path

import pytest

from bravo_iac import IacError, check_state, module, plan
from bravo_iac.chart import manifests
from bravo_iac.network import view
from bravo_iac.tailnet import check_policy, fragment, status, verify

REPO = Path(__file__).resolve().parents[3]
STATES = [json.loads(p.read_text()) for p in sorted((REPO / "infra/iac/environments").glob("*.json"))]
PLATFORM = json.loads((REPO / "infra/iac/environments/platform.json").read_text())
ACCESS = module("operator-access", "1")
CAP = {"tailscale.com/cap/kubernetes": [{"impersonate": {"groups": ["bravo:operators"]}}]}


def access_params(state=PLATFORM):
    return next(m for m in state["modules"] if m["id"] == "operator-access")["params"]


def test_platform_operators_are_one_group_bound_to_cluster_admin():
    [binding] = [o for o in manifests(PLATFORM)[0] if o["kind"] == "ClusterRoleBinding"]
    assert binding["metadata"]["name"] == "bravo-operators-cluster-admin" and "namespace" not in binding["metadata"]
    assert "spec" not in binding
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"}
    assert binding["subjects"] == [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": "bravo:operators"}]
    # No person is named in the repository: members of the tailnet group live in the tailnet policy.
    assert "@" not in json.dumps(PLATFORM)


def test_one_grant_opens_the_proxy_and_names_the_impersonated_group():
    policy = fragment(STATES)
    grant = {"src": ["group:bravo-operators"], "dst": ["tag:bravo-platform-operator"], "ip": ["tcp:443"], "app": CAP}
    assert grant in policy["grants"]
    # Route tests only: the group's members are outside the repository.
    assert not any(t["src"] == "group:bravo-operators" for t in policy["tests"])
    assert check_policy(policy, STATES) == []
    without_cap = copy.deepcopy(policy)
    without_cap["grants"] = [{k: v for k, v in g.items() if k != "app"} for g in without_cap["grants"]]
    assert check_policy(without_cap, STATES) == ["GRANT:group:bravo-operators->tag:bravo-platform-operator:tcp:443+app"]
    masters = copy.deepcopy(policy)
    for g in masters["grants"]:
        if "app" in g:
            g["app"] = {"tailscale.com/cap/kubernetes": [{"impersonate": {"groups": ["system:masters"]}}]}
    assert check_policy(masters, STATES) == ["GRANT:group:bravo-operators->tag:bravo-platform-operator:tcp:443+app"]


@pytest.mark.parametrize("change", [
    lambda p: p["bindings"][0].update(group="system:masters"),
    lambda p: p["bindings"][0].update(group="operators"),
    lambda p: p["bindings"][0].update(tailnet_group="autogroup:member"),
    lambda p: p["bindings"][0].update(cluster_role="Cluster Admin"),
    lambda p: p["bindings"].append(dict(p["bindings"][0])),
    lambda p: p["bindings"].append({**p["bindings"][0], "name": "second"}),
    lambda p: p.update(bindings=[]),
    lambda p: p.update(proxy_tag="bravo-platform-operator"),
    lambda p: p.update(tailnet="default"),
    lambda p: p["bindings"][0].update(extra=True),
])
def test_invalid_access_params_are_refused(change):
    params = copy.deepcopy(access_params())
    change(params)
    with pytest.raises(IacError, match="IAC_PARAMS_INVALID"):
        ACCESS.check(params)


@pytest.mark.parametrize("change", [
    lambda p: p["bindings"].append({"name": "bravo-viewers", "group": "bravo:viewers", "cluster_role": "view",
                                    "tailnet_group": "group:bravo-viewers"}),
    lambda p: p["bindings"][0].update(cluster_role="admin"),
    lambda p: p["bindings"][0].update(tailnet_group="group:everyone"),
    lambda p: p.update(tailnet="workload"),
])
def test_an_agent_cannot_widen_operator_access(change):
    widened = copy.deepcopy(PLATFORM)
    change(access_params(widened))
    result = plan(PLATFORM, widened, origin="agent")
    assert result["refusals"] == ["IAC_WIDENING_REFUSED"]
    assert plan(PLATFORM, widened, origin="operator")["accepted"]


def test_an_agent_may_remove_operator_access():
    two = copy.deepcopy(PLATFORM)
    access_params(two)["bindings"].append({"name": "bravo-viewers-view", "group": "bravo:viewers",
                                           "cluster_role": "view", "tailnet_group": "group:bravo-viewers"})
    result = plan(two, PLATFORM, origin="agent")
    assert result["accepted"] and not result["widens"]
    assert [c["action"] for c in result["changes"]] == ["delete"]


def test_verify_checks_the_api_proxy_device_and_its_tag():
    def peers(tags, online=True):
        return status(json.dumps({"BackendState": "Running", "Peer": {
            "p": {"HostName": "bravo-platform-operator", "Tags": tags, "Online": online}}}))

    def row(seen):
        return next(r for r in verify(STATES, seen) if r["kind"] == "api-proxy")

    assert row(peers(["tag:bravo-platform-operator"]))["result"] == "OK"
    assert row(peers(["tag:bravo-cutover-control"]))["result"] == "TAG_MISMATCH"
    assert row(peers(["tag:bravo-platform-operator"], online=False))["result"] == "OFFLINE"


def test_the_network_view_answers_how_people_reach_the_platform_api():
    access = view(STATES)["environments"]["platform"]["access"]
    assert access == [{"proxy": "bravo-platform-operator", "proxy_tag": "tag:bravo-platform-operator",
                       "tailnet": "default", "tailnet_group": "group:bravo-operators",
                       "group": "bravo:operators", "cluster_role": "cluster-admin"}]
    assert check_state(PLATFORM)["modules"][-1]["id"] == "operator-access"
