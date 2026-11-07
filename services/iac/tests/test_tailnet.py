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
    assert {"src": ["tag:bravo-cutover-control"], "dst": ["tag:bravo-target-control-pooler"],
            "ip": ["tcp:6432"]} in added["tailnet_grants"]


@pytest.mark.parametrize("change", [
    lambda p: p["groups"][0].update(tag="bravo-rehearsal"),
    lambda p: p["groups"][0].update(tag=p["owner_tag"]),
    lambda p: p["groups"][0]["routes"][0].update(port=0),
    lambda p: p["groups"][0]["routes"][0].update(destination="bravo-target-operator.example.ts.net"),
    lambda p: p["groups"][0]["clients"][0].update(labels={}),
    lambda p: p["groups"].append(copy.deepcopy(p["groups"][0])),
    lambda p: p["groups"][1]["routes"].append(copy.deepcopy(p["groups"][1]["routes"][0])),
    lambda p: p.update(extra=True),
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
