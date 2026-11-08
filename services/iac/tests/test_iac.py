"""T44 IaC catalog, plan, CLI and API on the committed target-dev desired state."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bravo_iac import IacError, check_state, describe, digest, module, plan
from bravo_iac.api import create_app
from bravo_iac.cli import main

REPO = Path(__file__).resolve().parents[3]
ENVIRONMENTS = REPO / "infra/iac/environments"
COMMITTED = json.loads((ENVIRONMENTS / "target-dev.json").read_text())


def closed(state=COMMITTED):
    proposed = copy.deepcopy(state)
    params = proposed["modules"][0]["params"]
    params["state"] = "closed"
    params.pop("demo_run_id")
    return proposed


def t9():
    spec = importlib.util.spec_from_file_location(
        "t9_object_exposure", REPO / "infra/envs/target-workloads/object_exposure.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.mark.parametrize("state", ["closed", "open-demo"])
def test_the_module_renders_the_same_identities_as_the_t9_renderer(state):
    environ = {"BRAVO_OBJECT_BOOTSTRAP_ACCESS_KEY": "A" * 20, "BRAVO_OBJECT_BOOTSTRAP_SECRET_KEY": "b" * 40,
               "BRAVO_OBJECT_CLIENT_ACCESS_KEY": "C" * 20, "BRAVO_OBJECT_CLIENT_SECRET_KEY": "d" * 40}
    t9_doc = t9().render(state, "demo-chat-attachments", environ, acknowledge_open=True,
                         demo_run_id="demo-2026-11-08")
    without_credentials = [{k: v for k, v in i.items() if k != "credentials"} for i in t9_doc["identities"]]
    params = {"state": state, "bucket": "demo-chat-attachments"}
    if state == "open-demo":
        params["demo_run_id"] = "demo-2026-11-08"
    [secret] = module("target-object-exposure", "1").render(params)
    assert secret["shape"]["identities"] == without_credentials


def test_the_committed_desired_state_is_valid_and_is_the_demo_fixture():
    state = check_state(COMMITTED)
    assert state["environment"] == "target-dev"
    assert state["modules"][0]["params"]["state"] == "open-demo"
    assert [m["id"] for m in describe()] == ["target-object-exposure", "tailnet-egress", "tailnet-ingress", "tailnet-operator",
                                            "operator-access", "api-transport-access", "workload-api-access", "cluster-audit",
                                            "service-account-issuer"]


def test_an_agent_may_plan_closing_the_bucket():
    result = plan(COMMITTED, closed(), origin="agent")
    assert result["accepted"] and not result["refusals"] and not result["widens"]
    [change] = result["changes"]
    assert change["resource"] == "Secret/target-dev/target-seaweedfs-auth" and change["action"] == "update"
    assert [i["name"] for i in change["before"]["identities"]] == ["bootstrap", "client", "anonymous"]
    assert [i["name"] for i in change["after"]["identities"]] == ["bootstrap", "client"]
    assert change["restarts"] == ["Deployment/target-dev/target-seaweedfs"]
    assert change["secret_inputs"] == ["bootstrap", "client"]
    [effect] = result["effects"]
    assert effect["anonymous_access"] == {"before": ["Read:demo-chat-attachments"], "after": []}
    assert effect["checks"] == [{"control": "IAM-01.1", "anonymous_get": "DENIED"}]
    assert result["authorizes_apply"] is False
    assert "secretKey" not in json.dumps(result) and "accessKey" not in json.dumps(result)


def test_an_agent_may_not_widen_exposure_an_operator_may():
    reopen = copy.deepcopy(COMMITTED)
    agent = plan(closed(), reopen, origin="agent")
    assert agent["widens"] and agent["refusals"] == ["IAC_WIDENING_REFUSED"] and not agent["accepted"]
    operator = plan(closed(), reopen, origin="operator")
    assert operator["widens"] and operator["accepted"]


def test_an_agent_may_not_add_or_remove_modules():
    empty = dict(COMMITTED, modules=[])
    result = plan(COMMITTED, empty, origin="agent")
    assert "IAC_MODULE_SET_CHANGE_REFUSED" in result["refusals"]
    assert result["changes"][0]["action"] == "delete"


def test_the_same_inputs_always_give_the_same_plan():
    a = plan(COMMITTED, closed(), origin="agent")
    reordered = json.loads(json.dumps(closed(), sort_keys=True))
    reordered["modules"][0] = dict(reversed(list(reordered["modules"][0].items())))
    assert plan(COMMITTED, reordered, origin="agent") == a
    assert a["plan_digest"] == digest({k: v for k, v in a.items() if k != "plan_digest"})
    assert plan(COMMITTED, COMMITTED, origin="agent")["changes"] == []


@pytest.mark.parametrize("change, code", [
    (lambda s: s["modules"][0]["params"].pop("demo_run_id"), "IAC_PARAMS_INVALID"),
    (lambda s: s["modules"][0]["params"].update(state="closed"), "IAC_PARAMS_INVALID"),
    (lambda s: s["modules"][0]["params"].update(bucket="*"), "IAC_PARAMS_INVALID"),
    (lambda s: s["modules"][0]["params"].update(extra=1), "IAC_PARAMS_INVALID"),
    (lambda s: s["modules"][0].update(version="2"), "IAC_MODULE_UNKNOWN"),
    (lambda s: s["modules"].append(copy.deepcopy(s["modules"][0])), "IAC_MODULE_DUPLICATE"),
    (lambda s: s.update(schema_version="2"), "IAC_STATE_INVALID"),
    (lambda s: s.update(environment="Target Dev"), "IAC_STATE_INVALID"),
])
def test_invalid_states_are_refused_with_a_fixed_code(change, code):
    state = copy.deepcopy(COMMITTED)
    change(state)
    with pytest.raises(IacError) as error:
        check_state(state)
    assert error.value.code == code


def test_plans_stay_within_one_environment():
    with pytest.raises(IacError, match="IAC_ENVIRONMENT_MISMATCH"):
        plan(COMMITTED, dict(closed(), environment="source-dev"), origin="operator")
    with pytest.raises(IacError, match="IAC_ORIGIN_INVALID"):
        plan(COMMITTED, closed(), origin="model")


def test_cli_plans_and_stages_only_what_was_planned(tmp_path, capsys):
    target = tmp_path / "target-dev.json"
    target.write_text(json.dumps(COMMITTED))
    proposed = tmp_path / "proposed.json"
    proposed.write_text(json.dumps(closed()))
    assert main(["plan", "--current", str(target), "--proposed", str(proposed), "--origin", "agent"]) == 0
    planned = tmp_path / "plan.json"
    planned.write_text(capsys.readouterr().out)
    assert main(["stage", "--plan", str(planned), "--proposed", str(proposed), "--target", str(target)]) == 0
    assert check_state(json.loads(target.read_text()))["modules"][0]["params"]["state"] == "closed"
    capsys.readouterr()
    # Git moved (the target now differs from the plan's base): stage refuses.
    assert main(["stage", "--plan", str(planned), "--proposed", str(proposed), "--target", str(target)]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "REFUSED", "code": "IAC_BASE_CHANGED"}
    target.write_text(json.dumps(closed()))
    proposed.write_text(json.dumps(COMMITTED))
    assert main(["plan", "--current", str(target), "--proposed", str(proposed), "--origin", "agent"]) == 1
    assert json.loads(capsys.readouterr().out)["refusals"] == ["IAC_WIDENING_REFUSED"]


def test_api_serves_the_catalog_the_committed_state_and_plans():
    client = TestClient(create_app(ENVIRONMENTS))
    assert client.get("/v1/iac/modules").json()["modules"][0]["id"] == "target-object-exposure"
    committed = client.get("/v1/iac/environments/target-dev").json()
    assert committed["digest"] == digest(check_state(COMMITTED))
    response = client.post("/v1/iac/plan", json={"environment": "target-dev", "proposed": closed(),
                                                 "origin": "agent"})
    assert response.status_code == 200 and response.json()["base_digest"] == committed["digest"]
    assert client.get("/v1/iac/environments/nowhere-dev").status_code == 404
    assert client.get("/v1/iac/environments/..%2Fsecrets").status_code == 404
    bad = client.post("/v1/iac/plan", json={"environment": "target-dev", "proposed": {}, "origin": "agent"})
    assert bad.status_code == 422 and bad.json()["detail"] == {"code": "IAC_STATE_INVALID"}
    assert client.post("/v1/iac/plan", json={"proposed": closed()}).status_code == 422


def test_the_agent_port_plans_one_modules_params_and_never_widens():
    from bravo_iac.agent import AgentPlanner

    port = AgentPlanner(COMMITTED)
    assert "target-object-exposure" in port.describe() and "secretKey" not in port.describe()
    result = port.plan("target-object-exposure", {"state": "closed", "bucket": "demo-chat-attachments"})
    assert result["origin"] == "agent" and result["accepted"] and result["base_digest"] == port.digest
    reopen = AgentPlanner(closed()).plan("target-object-exposure", COMMITTED["modules"][0]["params"])
    assert reopen["refusals"] == ["IAC_WIDENING_REFUSED"]
    with pytest.raises(IacError, match="IAC_MODULE_NOT_IN_ENVIRONMENT"):
        port.plan("target-app", {})
    with pytest.raises(IacError, match="IAC_PARAMS_INVALID"):
        port.plan("target-object-exposure", {"state": "closed", "bucket": "demo-chat-attachments",
                                             "demo_run_id": "demo-2026-11-08"})
