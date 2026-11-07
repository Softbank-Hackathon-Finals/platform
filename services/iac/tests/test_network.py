"""T44 one network API: exposed devices (tailnet-ingress), cluster-side routes moved from the old
renderers, and the single view of every declared route (GET /v1/iac/network, `bravo-iac network`)."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from bravo_iac import IacError, check_state, module, plan
from bravo_iac.chart import manifests
from bravo_iac.cli import main
from bravo_iac.network import view
from bravo_iac.tailnet import fragment

REPO = Path(__file__).resolve().parents[3]
ENVIRONMENTS = {p.stem: json.loads(p.read_text()) for p in sorted((REPO / "infra/iac/environments").glob("*.json"))}
STATES = list(ENVIRONMENTS.values())


def legacy(path, name):
    spec = importlib.util.spec_from_file_location(name, REPO / path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


T8 = legacy("infra/envs/target-workloads/network.py", "t8_network")
T12 = legacy("infra/envs/executor-network/control_network.py", "t12_control_network")


def objects(environment):
    """kind/namespace/name -> the generated manifest, with a sample tailnet in place of the value."""
    rendered, _ = manifests(ENVIRONMENTS[environment])
    text = json.dumps(rendered).replace("{tailnet}", "sample.ts.net")
    return {f'{o["kind"]}/{o["metadata"].get("namespace", "-")}/{o["metadata"]["name"]}': o for o in json.loads(text)}


def same_route(declared, old):
    assert declared["metadata"]["annotations"] == old["metadata"]["annotations"]
    for key in ("selector", "ports", "type", "externalName"):
        assert declared["spec"].get(key) == old["spec"].get(key), key


@pytest.mark.parametrize(("side", "peer"), [("source", "target"), ("target", "source")])
def test_the_declared_cluster_routes_are_what_the_old_renderers_produced(side, peer):
    rendered = objects(f"{side}-dev")
    same_route(rendered[f"Service/{side}-dev/{side}-db-tailnet"], T8.ingress(side))
    same_route(rendered[f"Service/{side}-dev/{side}-control-pooler-tailnet"], T12.pooler_ingress(side))
    for old in T8.egress(side, f"bravo-{peer}-db.sample.ts.net"):
        key = f'{old["kind"]}/{old["metadata"].get("namespace", "-")}/{old["metadata"]["name"]}'
        if old["kind"] == "ProxyGroup":
            assert rendered[key]["spec"] == old["spec"]
        else:
            same_route(rendered[key], old)


def test_replication_and_pooler_clients_keep_their_own_egress():
    # CNPG instances and the pooler also reach their own DB, backups and the API server: the module
    # restricts only the proxy side to them and never isolates their egress.
    rendered = objects("target-dev")
    assert not any(k.startswith("NetworkPolicy/target-dev/") for k in rendered)
    proxy = rendered["NetworkPolicy/tailscale-system/bravo-target-pooler-ingress"]["spec"]["ingress"][1]
    assert proxy["from"][0]["podSelector"]["matchLabels"] == {"app": "target-pgbouncer"}
    assert "NetworkPolicy/bravo-observability/bravo-target-collector-client" in rendered


def test_the_old_cli_stages_are_refused():
    import subprocess
    import sys
    for stage in ("ingress", "egress"):
        done = subprocess.run([sys.executable, str(REPO / "infra/envs/target-workloads/network.py"), stage,
                               "--side", "source"], capture_output=True, text=True, check=False)
        assert done.returncode == 2 and "infra/iac/environments" in done.stderr
    assert T12.MOVED_TO_IAC >= {"pooler-ingress"}


def test_device_tags_are_owned_by_their_cluster_operator_and_granted_to_the_routes():
    policy = fragment(STATES)
    assert policy["tagOwners"]["tag:bravo-source-db"] == ["tag:bravo-source-operator"]
    assert policy["tagOwners"]["tag:bravo-target-control-pooler"] == ["tag:bravo-target-operator"]
    assert {"src": ["tag:bravo-target-replication"], "dst": ["tag:bravo-source-db"], "ip": ["tcp:5432"]} in policy["grants"]
    assert {"src": ["tag:bravo-source-replication"], "dst": ["tag:bravo-target-db"], "ip": ["tcp:5432"]} in policy["grants"]
    committed = json.loads((REPO / "infra/tailnet/routes-policy.json").read_text())
    assert committed == policy


INGRESS = module("tailnet-ingress", "1")


@pytest.mark.parametrize("change", [
    lambda d: d[0]["ports"].append({"name": "admin", "port": 9187, "target_port": 9187}),
    lambda d: d[0]["selector"].pop("cnpg.io/instanceRole"),           # every instance, not the primary
    lambda d: d[1]["ports"][0].update(target_port=6432),
    lambda d: d.append({**copy.deepcopy(d[0]), "service": "source-db-replica", "hostname": "bravo-source-db-ro"}),
    lambda d: d[0].update(tailnet="workload"),
])
def test_an_agent_cannot_widen_what_the_tailnet_reaches(change):
    proposed = copy.deepcopy(ENVIRONMENTS["source-dev"])
    change(next(m for m in proposed["modules"] if m["id"] == "tailnet-ingress")["params"]["devices"])
    result = plan(ENVIRONMENTS["source-dev"], proposed, origin="agent")
    assert result["refusals"] == ["IAC_WIDENING_REFUSED"]
    assert plan(ENVIRONMENTS["source-dev"], proposed, origin="operator")["accepted"]


def test_an_agent_may_take_a_device_off_the_tailnet_but_not_drop_a_client_policy():
    fewer = copy.deepcopy(ENVIRONMENTS["source-dev"])
    next(m for m in fewer["modules"] if m["id"] == "tailnet-ingress")["params"]["devices"].pop()
    assert plan(ENVIRONMENTS["source-dev"], fewer, origin="agent")["accepted"]
    open_collector = copy.deepcopy(ENVIRONMENTS["source-dev"])
    next(m for m in open_collector["modules"] if m["id"] == "tailnet-egress")["params"]["groups"][0]["client_policy"] = False
    assert plan(ENVIRONMENTS["source-dev"], open_collector, origin="agent")["refusals"] == ["IAC_WIDENING_REFUSED"]


@pytest.mark.parametrize("change", [
    lambda d: d[0].update(tag="tag:bravo-source-operator"),
    lambda d: d[1].update(hostname=d[0]["hostname"]),
    lambda d: d[0]["ports"].append(dict(d[0]["ports"][0])),
    lambda d: d[0]["ports"][0].update(port=0),
    lambda d: d[0].update(selector={}),
    lambda d: d[0].update(tailnet="default"),
    lambda d: d[0].update(extra=True),
])
def test_invalid_devices_are_refused(change):
    state = copy.deepcopy(ENVIRONMENTS["source-dev"])
    change(next(m for m in state["modules"] if m["id"] == "tailnet-ingress")["params"]["devices"])
    with pytest.raises(IacError, match="IAC_PARAMS_INVALID"):
        check_state(state)


def test_client_policy_only_takes_false():
    state = copy.deepcopy(ENVIRONMENTS["source-dev"])
    next(m for m in state["modules"] if m["id"] == "tailnet-egress")["params"]["groups"][0]["client_policy"] = True
    with pytest.raises(IacError, match="IAC_PARAMS_INVALID"):
        check_state(state)
    assert INGRESS.grants({"owner_tag": "tag:x", "devices": []}) == []


def test_one_view_answers_how_each_cluster_reaches_each_device():
    network = view(STATES)
    assert set(network["environments"]) == set(ENVIRONMENTS) and set(network["tailnets"]) == {"default", "workload"}
    target = network["environments"]["target-dev"]
    routes = {r["service"]: r for r in target["egress"]}
    replication = routes["source-db-for-replication.target-dev.svc.cluster.local"]
    assert (replication["destination"], replication["port"], replication["tailnet"]) == ("bravo-source-db", 5432, "default")
    assert replication["client_policy"] is False
    devices = {d["hostname"]: d for d in network["environments"]["source-dev"]["ingress"]}
    assert devices["bravo-source-control-pooler"]["ports"] == [{"name": "pooler", "port": 6432, "target_port": 5432}]
    # Every egress destination declared on the default tailnet is a device some state exposes or a
    # host outside these clusters (metrics store, target API, operators).
    exposed = {d["hostname"] for env in network["environments"].values() for d in env["ingress"]}
    assert {"bravo-source-db", "bravo-target-db", "bravo-source-control-pooler", "bravo-target-control-pooler"} <= exposed
    assert network["tailnets"]["workload"] == json.loads((REPO / "infra/tailnet/routes-policy.workload.json").read_text())
    with pytest.raises(IacError, match="IAC_ENVIRONMENT_DUPLICATE"):
        view([ENVIRONMENTS["source-dev"], ENVIRONMENTS["source-dev"]])
    assert ".ts.net" not in json.dumps(network)


def test_the_api_and_the_cli_give_the_same_view(capsys):
    from fastapi.testclient import TestClient

    from bravo_iac.api import create_app

    client = TestClient(create_app(REPO / "infra/iac/environments"))
    answer = client.get("/v1/iac/network")
    assert answer.status_code == 200 and answer.json() == view(STATES)
    assert main(["network", *[str(p) for p in sorted((REPO / "infra/iac/environments").glob("*.json"))]]) == 0
    assert json.loads(capsys.readouterr().out) == answer.json()
