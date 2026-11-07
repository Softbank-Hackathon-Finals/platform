import copy
import json
from pathlib import Path

import pytest

from bravo_iac import IacError, module, plan
from bravo_iac.network import view
from bravo_iac.tailnet import check_policy, fragment, verify

REPO = Path(__file__).resolve().parents[3]
PARAMS = {"proxy_hostname": "bravo-source-operator", "proxy_tag": "tag:bravo-source-operator",
          "tailnet_groups": ["group:bravo-operators"]}


def state(params=PARAMS):
    return {"schema_version": "1", "environment": "source-dev", "modules": [
        {"id": "api-transport-access", "version": "1", "params": copy.deepcopy(params)}]}


def test_transport_only_never_creates_roles_or_impersonation():
    found = module("api-transport-access", "1")
    checked = found.check(PARAMS)
    assert found.render(checked) == []
    grants = found.grants(checked)
    assert grants == [{"tailnet": "default", "src": ["group:bravo-operators"],
                       "dst": ["tag:bravo-source-operator"], "ip": ["tcp:443"]}]
    assert "app" not in grants[0]


@pytest.mark.parametrize("change", [
    lambda p: p.update(tailnet_groups=["*"]),
    lambda p: p.update(tailnet_groups=["autogroup:member"]),
    lambda p: p.update(tailnet_groups=[]),
    lambda p: p.update(tailnet_groups=["group:bravo-operators"]*2),
    lambda p: p.update(proxy_hostname="https://foreign.invalid"),
    lambda p: p.update(proxy_tag="*"),
    lambda p: p.update(cluster_role="cluster-admin"),
    lambda p: p.update(token="not-an-accepted-field"),
    lambda p: p.update(port=6443),
])
def test_refuses_broad_subjects_credentials_roles_and_extra_fields(change):
    params = copy.deepcopy(PARAMS)
    change(params)
    with pytest.raises(IacError, match="IAC_PARAMS_INVALID"):
        module("api-transport-access", "1").check(params)


def test_agent_cannot_add_reachability_but_operator_plan_is_deterministic():
    current = state()
    proposed = state(dict(PARAMS, tailnet_groups=["group:bravo-operators", "autogroup:owner"]))
    denied = plan(current, proposed, origin="agent")
    assert denied["accepted"] is False and "IAC_WIDENING_REFUSED" in denied["refusals"]
    accepted = plan(current, proposed, origin="operator")
    assert accepted["accepted"] and accepted["authorizes_apply"] is False
    assert accepted == plan(current, proposed, origin="operator")
    other_tailnet = state(dict(PARAMS, tailnet="workload"))
    assert not plan(current, other_tailnet, origin="agent")["accepted"]


def test_human_network_grants_do_not_generate_tag_only_policy_tests():
    value = fragment([state()])
    assert value["grants"] == [{"src": ["group:bravo-operators"], "dst": ["tag:bravo-source-operator"], "ip": ["tcp:443"]}]
    assert value["tests"] == []
    assert fragment([state(dict(PARAMS, tailnet="workload"))])["grants"] == []


def test_transport_proxy_is_checked_for_registration_and_liveness():
    missing = verify([state()], {})
    assert missing == [{"kind": "api-proxy", "host": "bravo-source-operator",
                        "tag": "tag:bravo-source-operator", "result": "NOT_VISIBLE"}]
    found = verify([state()], {"bravo-source-operator": {"tags": ["tag:bravo-source-operator"], "online": True}})
    assert found[0]["result"] == "OK"


def test_source_transport_does_not_declare_an_rbac_binding():
    source = json.loads((REPO / "infra/iac/environments/source-dev.json").read_text())
    assert any(m["id"] == "api-transport-access" for m in source["modules"])
    assert all(m["id"] != "operator-access" for m in source["modules"])


def test_network_view_distinguishes_reachability_from_kubernetes_authorization():
    result = view([state()])
    assert result["environments"]["source-dev"]["access"] == [
        {"proxy": "bravo-source-operator", "proxy_tag": "tag:bravo-source-operator",
         "tailnet": "default", "tailnet_groups": ["group:bravo-operators"],
         "authentication": "existing-kubernetes-auth"}]
    assert "default" in result["tailnets"]
    assert all("app" not in g for g in result["tailnets"]["default"]["grants"])


def test_transport_only_proxy_cannot_silently_acquire_impersonation_grants():
    policy = fragment([state()])
    assert check_policy(policy, [state()]) == []
    policy["grants"][0]["app"] = {
        "tailscale.com/cap/kubernetes": [{"impersonate": {"groups": ["system:masters"]}}]}
    assert check_policy(policy, [state()]) == [
        "SYSTEM_IMPERSONATION:group:bravo-operators",
        "UNDECLARED_GRANT:group:bravo-operators->tag:bravo-source-operator+app"]

