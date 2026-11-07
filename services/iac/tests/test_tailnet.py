"""T44 tailnet-egress module, derived tailnet policy and the live check through Tailscale (status JSON)."""

import copy
import json
from pathlib import Path

import pytest

from bravo_iac import IacError, check_state, module, plan
from bravo_iac.cli import main
from bravo_iac.tailnet import check_policy, fragment, status, verify

REPO = Path(__file__).resolve().parents[3]
ENVIRONMENTS = sorted((REPO / "infra/iac/environments").glob("*.json"))
STATES = [json.loads(p.read_text()) for p in ENVIRONMENTS]
PLATFORM = json.loads((REPO / "infra/iac/environments/platform.json").read_text())
EGRESS = module("tailnet-egress", "1")


def resources(state=PLATFORM):
    entry = check_state(state)["modules"][0]
    return {f'{r["kind"]}/{r["namespace"]}/{r["name"]}': r for r in EGRESS.render(entry["params"])}


def test_routes_are_rendered_without_anything_observed_after_apply():
    rendered = resources()
    client = rendered["NetworkPolicy/bravo-platform/bravo-cutover-control-client"]["shape"]
    to_proxy = client["egress"][1]
    assert to_proxy["ports"] == [{"protocol": "TCP", "port": 10000, "endPort": 10999}]
    assert to_proxy["to"][0]["podSelector"]["matchLabels"] == {
        "tailscale.com/parent-resource": "bravo-cutover-control", "tailscale.com/parent-resource-type": "proxygroup"}
    ingress = rendered["NetworkPolicy/tailscale-system/bravo-cutover-control-ingress"]["shape"]
    assert ingress["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 9002}]
    service = rendered["Service/bravo-platform/target-pooler-for-cutover"]["shape"]
    assert service["annotations"]["tailscale.com/tailnet-fqdn"] == "bravo-target-control-pooler.{tailnet}"
    assert rendered["ProxyGroup/-/bravo-cutover-control"]["shape"]["tags"] == ["tag:bravo-cutover-control"]
    # Two client selectors in one namespace each get a policy; no observed port anywhere.
    assert "NetworkPolicy/platform-state/bravo-rehearsal-egress-client" in rendered
    assert ".ts.net" not in json.dumps(rendered)


def test_the_committed_policy_fragment_is_the_render_of_the_committed_states():
    committed = json.loads((REPO / "infra/tailnet/routes-policy.json").read_text())
    assert committed == fragment(STATES)
    assert check_policy(committed, STATES) == []
    assert {"src": ["tag:bravo-cutover-control"], "dst": ["tag:bravo-target-db"], "ip": ["tcp:5432"]} in committed["grants"]
    [cutover] = [t for t in committed["tests"] if t["src"] == "tag:bravo-cutover-control"]
    assert "tag:bravo-source-control-pooler:6432" in cutover["accept"] and "tag:bravo-source-db:22" in cutover["deny"]


def test_a_policy_missing_a_route_is_reported_by_code():
    repo_fragment = json.loads((REPO / "infra/envs/collector-network/tailnet-fragment.json").read_text())
    missing = check_policy(repo_fragment, STATES)
    assert "GRANT:tag:bravo-cutover-control->tag:bravo-target-db:tcp:5432" in missing
    assert not any("tag:bravo-source-collector->tag:bravo-metrics" in m for m in missing if m.startswith("GRANT"))
    assert "TAG_OWNER:tag:bravo-cutover-control" in missing
    with pytest.raises(IacError, match="IAC_POLICY_INVALID"):
        check_policy([], STATES)


def test_an_agent_may_remove_a_route_but_never_add_one():
    fewer = copy.deepcopy(PLATFORM)
    routes = fewer["modules"][0]["params"]["groups"][3]["routes"]
    routes.pop()
    removed = plan(PLATFORM, fewer, origin="agent")
    assert removed["accepted"] and removed["effects"][0]["reach"]["added"] == []
    assert any(c["action"] == "delete" for c in removed["changes"])
    added = plan(fewer, PLATFORM, origin="agent")
    assert added["refusals"] == ["IAC_WIDENING_REFUSED"] and added["effects"][0]["reach"]["added"]
    assert plan(fewer, PLATFORM, origin="operator")["accepted"]
    assert {"tailnet": "default", "src": ["tag:bravo-cutover-control"], "dst": ["tag:bravo-target-operator"],
            "ip": ["tcp:443"]} in added["tailnet_grants"]


@pytest.mark.parametrize("change", [
    lambda groups: groups[1].pop("tailnet"),                      # apply routes onto the default tailnet
    lambda groups: groups[3].update(tailnet="workload"),          # cutover routes onto the workload tailnet
    lambda groups: groups[1].update(tailnet="other"),
])
def test_an_agent_cannot_move_a_group_to_another_tailnet(change):
    # The same host name on another tailnet is another device: the route's destination changes.
    moved = copy.deepcopy(PLATFORM)
    change(moved["modules"][0]["params"]["groups"])
    result = plan(PLATFORM, moved, origin="agent")
    assert result["refusals"] == ["IAC_WIDENING_REFUSED"]
    reach = next(e["reach"] for e in result["effects"] if "reach" in e)
    assert reach["added"] and reach["removed"]
    assert plan(PLATFORM, moved, origin="operator")["accepted"]


def test_an_agent_cannot_give_a_group_another_tag():
    # Same hosts on the cluster side, but the tailnet policy would have to grant the new tag (an
    # existing tag: scripts/tests/test_collector_route denies every tag named in this repository).
    retagged = copy.deepcopy(PLATFORM)
    retagged["modules"][0]["params"]["groups"][1]["tag"] = "tag:bravo-loadgen"
    result = plan(PLATFORM, retagged, origin="agent")
    assert result["refusals"] == ["IAC_WIDENING_REFUSED"]
    [grants] = [e["tailnet_grants"] for e in result["effects"] if "tailnet_grants" in e]
    assert any('"tag:bravo-loadgen"' in g for g in grants["added"])
    assert not any("reach" in e for e in result["effects"])


def test_groups_on_another_tailnet_get_their_own_suffix_and_policy():
    rendered = resources()
    apply = rendered["Service/bravo-apply-control/target-api"]["shape"]["annotations"]
    assert apply["tailscale.com/tailnet-fqdn"] == "bravo-target-api.{tailnet:workload}"
    cutover = rendered["Service/bravo-platform/target-api-for-cutover"]["shape"]["annotations"]
    assert cutover["tailscale.com/tailnet-fqdn"] == "bravo-target-operator.{tailnet}"
    default, workload = fragment(STATES), fragment(STATES, "workload")
    assert not any(g["src"] == ["tag:bravo-apply"] for g in default["grants"])
    assert {"src": ["tag:bravo-apply"], "dst": ["tag:bravo-target-api"], "ip": ["tcp:443"]} in workload["grants"]
    assert "tag:bravo-cutover-control" in default["tagOwners"] and "tag:bravo-cutover-control" not in workload["tagOwners"]
    committed = json.loads((REPO / "infra/tailnet/routes-policy.workload.json").read_text())
    assert committed == workload and check_policy(committed, STATES, "workload") == []
    assert check_policy(committed, STATES) != []  # the workload policy is not the default tailnet's


def test_platform_routes_are_the_live_route_services():
    # 2026-11-07 13:00 KST readback of the platform cluster: ExternalName egress Services and their tailnet.
    live = {
        "bravo-apply-control/target-api": "workload", "bravo-rehearsal-control/target-api": "workload",
        "platform-state/collector-target-api": "workload",
        **{f"bravo-platform/{side}-{kind}-for-cutover": "default"
           for side in ("source", "target") for kind in ("api", "db", "pooler")},
        # 2026-11-07 16:10 KST readback: the credential issuer route (kubectl-created 11-06), now declared.
        "bravo-rehearsal-control/target-credentials": "workload",
    }
    # Declared before it exists: the T31 apply identity's route to the same shared issuer.
    new = {"bravo-apply-control/target-credentials": "workload"}
    groups = check_state(PLATFORM)["modules"][0]["params"]["groups"]
    declared = {f'{r["namespace"]}/{r["service"]}': g.get("tailnet", "default") for g in groups for r in g["routes"]}
    assert declared == live | new


@pytest.mark.parametrize("change", [
    lambda p: p["groups"][0].update(tag="bravo-rehearsal"),
    lambda p: p["groups"][0].update(tag=p["owner_tag"]),
    lambda p: p["groups"][0]["routes"][0].update(port=0),
    lambda p: p["groups"][0]["routes"][0].update(destination="bravo-target-operator.example.ts.net"),
    lambda p: p["groups"][0]["clients"][0].update(labels={}),
    lambda p: p["groups"].append(copy.deepcopy(p["groups"][0])),
    lambda p: p["groups"][1]["routes"].append(copy.deepcopy(p["groups"][1]["routes"][0])),
    lambda p: p.update(extra=True),
    lambda p: p["groups"][0].update(tailnet="default"),
    lambda p: p["groups"][0].update(tailnet="Workload"),
])
def test_invalid_egress_params_are_refused(change):
    state = copy.deepcopy(PLATFORM)
    change(state["modules"][0]["params"])
    with pytest.raises(IacError, match="IAC_PARAMS_INVALID"):
        check_state(state)


def peers_json(peers, running=True):
    return json.dumps({"BackendState": "Running" if running else "Stopped",
                       "Peer": {str(i): {"HostName": h, "Tags": t, "Online": o} for i, (h, t, o) in enumerate(peers)}})


def test_verify_judges_what_this_device_sees_through_tailscale():
    seen = status(peers_json([("bravo-target-operator", ["tag:bravo-target-api"], True),
                              ("bravo-cutover-control-0", ["tag:bravo-cutover-control"], True),
                              ("bravo-source-db", ["tag:bravo-source-db"], False)]))
    rows = {(r["kind"], r["host"]): r["result"] for r in verify(STATES, seen)}
    assert rows[("destination", "bravo-target-operator")] == "TAG_MISMATCH"
    assert rows[("proxy-group", "bravo-cutover-control")] == "OK"
    assert rows[("destination", "bravo-source-db")] == "OFFLINE"
    assert rows[("destination", "bravo-metrics")] == "NOT_VISIBLE"
    with pytest.raises(IacError, match="IAC_TAILNET_NOT_RUNNING"):
        status(peers_json([], running=False))


def test_cli_renders_checks_and_verifies(tmp_path, capsys):
    paths = [str(p) for p in ENVIRONMENTS]
    assert main(["tailnet-policy", *paths]) == 0
    assert json.loads(capsys.readouterr().out) == fragment(STATES)
    assert main(["tailnet-check", "--policy", str(REPO / "infra/tailnet/routes-policy.json"), *paths]) == 0
    capsys.readouterr()
    saved = tmp_path / "status.json"
    saved.write_text(peers_json([("bravo-target-operator", ["tag:bravo-target-operator"], True)]))
    assert main(["tailnet-verify", "--status-json", str(saved), *paths]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_a_new_cluster_declares_its_operator_tag_owned_by_admins_and_nothing_else():
    """ADR-0035: the tag exists in the policy before the federated identity that joins with it."""
    from bravo_iac import module

    found = module("tailnet-operator", "1")
    params = found.check({"tag": "tag:bravo-target-gke-operator"})
    assert found.tag_owners(params) == [{"tailnet": "default", "tag": "tag:bravo-target-gke-operator",
                                          "owner": "autogroup:admin"}]
    assert found.grants(params) == [] and found.render(params) == []
    assert found.exposure(params) == {"anonymous": [], "reach": [], "checks": []}
    for bad in ({"tag": "tag:bravo-target-gke-db"}, {"tag": "tag:other-operator"}, {"tag": "tag:bravo-x-operator", "owner": "x"},
                {}, {"tag": "tag:bravo-target-gke-operator", "tailnet": "Bad Name"}):
        with pytest.raises(IacError):
            found.check(bad)
    committed = json.loads((REPO / "infra/tailnet/routes-policy.json").read_text())
    granted = {f"{g['dst'][0]}:{g['ip'][0].removeprefix('tcp:')}" for g in committed["grants"] if "app" not in g}
    for env in ("target-gke", "target-gce", "t49"):
        tag = f"tag:bravo-{env}-operator"
        assert committed["tagOwners"][tag] == ["autogroup:admin"]
        assert not [g for g in committed["grants"] if tag in g["src"]]  # the path to it is the network API's (#622)
        [test] = [t for t in committed["tests"] if t["src"] == tag]
        # Runtime device names escape a repository scan: the policy refuses every granted destination.
        assert set(test["deny"]) == granted and test["accept"] == [] and "tag:bravo-metrics:9090" in test["deny"]
