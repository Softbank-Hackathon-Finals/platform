"""T44 cluster-audit: the API server audit policy as a node file, its bundle, API and applier checks."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bravo_iac import IacError, module, plan
from bravo_iac.api import create_app
from bravo_iac.chart import files as chart_files
from bravo_iac.chart import manifests
from bravo_iac.node import files, listing, read

REPO = Path(__file__).resolve().parents[3]
CANONICAL = REPO / "services/control-runner/templates/rke2-audit-policy.yaml"
AUDIT = module("cluster-audit", "1")
RKE2 = ("platform", "target-dev")


def state(environment):
    return json.loads((REPO / f"infra/iac/environments/{environment}.json").read_text())


def test_the_packaged_policy_is_the_reviewed_policy_byte_for_byte():
    assert (REPO / "services/iac/src/bravo_iac/data/rke2-audit-policy.yaml").read_bytes() == CANONICAL.read_bytes()


@pytest.mark.parametrize("environment", RKE2)
def test_rke2_environments_declare_the_audit_policy_and_commit_its_bundle(environment):
    generated = files(state(environment))
    committed = {str(p.relative_to(REPO / "infra/iac/nodes" / environment)): p.read_text()
                 for p in (REPO / "infra/iac/nodes" / environment).rglob("*") if p.is_file()}
    assert committed == generated
    assert generated["etc/rancher/rke2/audit-policy.yaml"] == CANONICAL.read_text()
    [row] = json.loads(generated["manifest.json"])["files"]
    assert row["path"] == "/etc/rancher/rke2/audit-policy.yaml" and row["mode"] == "0600"
    assert row["restart"] == ["StaticPod/kube-system/kube-apiserver"]


def test_the_source_eks_cluster_has_no_node_file_module():
    assert all(m["id"] != "cluster-audit" for m in state("source-dev")["modules"])
    with pytest.raises(IacError, match="IAC_RUNTIME_UNSUPPORTED"):
        AUDIT.check({"runtime": "eks", "policy": "metadata-v1"})


@pytest.mark.parametrize("params", [
    {"runtime": "rke2"}, {"runtime": "rke2", "policy": "none"}, {"runtime": "rke2", "policy": "metadata-v1", "x": 1},
    {"runtime": ["rke2"], "policy": "metadata-v1"},
])
def test_invalid_audit_params_are_refused(params):
    with pytest.raises(IacError):
        AUDIT.check(params)


def test_the_chart_leaves_node_files_to_their_own_applier():
    objects, unmanaged = manifests(state("platform"))
    assert not any(o["kind"] == "NodeFile" for o in objects)
    assert "cluster-audit:NodeFile/-/audit-policy" in unmanaged
    assert "NodeFile" not in chart_files(state("platform"))["templates/resources.yaml"]


def test_an_agent_cannot_drop_the_audit_policy():
    dropped = copy.deepcopy(state("platform"))
    dropped["modules"] = [m for m in dropped["modules"] if m["id"] != "cluster-audit"]
    result = plan(state("platform"), dropped, origin="agent")
    assert "IAC_MODULE_SET_CHANGE_REFUSED" in result["refusals"]
    [change] = [c for c in result["changes"] if c["module"] == "cluster-audit"]
    assert change["action"] == "delete" and change["restarts"] == ["StaticPod/kube-system/kube-apiserver"]


def test_the_api_lists_and_serves_node_files():
    client = TestClient(create_app(REPO / "infra/iac/environments"))
    listed = client.get("/v1/iac/environments/platform/node-files")
    assert listed.status_code == 200 and listed.json() == listing(state("platform"))
    [row] = listed.json()["files"]
    assert row["href"] == "/v1/iac/environments/platform/node-files/etc/rancher/rke2/audit-policy.yaml"
    one = client.get(row["href"])
    assert one.status_code == 200 and one.json()["content"] == CANONICAL.read_text()
    assert one.json() == read(state("platform"), "/etc/rancher/rke2/audit-policy.yaml")
    assert client.get("/v1/iac/environments/platform/node-files/etc/shadow").status_code == 404
    assert client.get("/v1/iac/environments/nowhere/node-files").status_code == 404
    assert client.get("/v1/iac/environments/source-dev/node-files").json()["files"] == []


def applier():
    spec = importlib.util.spec_from_file_location("node_apply", REPO / "infra/iac/nodes/apply.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_the_applier_refuses_a_bundle_whose_bytes_differ_from_its_manifest(tmp_path):
    apply = applier()
    for name, content in files(state("platform")).items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(content)
    [row] = apply.bundle(tmp_path)
    assert row["data"] == CANONICAL.read_bytes()
    (tmp_path / "etc/rancher/rke2/audit-policy.yaml").write_text("rules:\n- level: None\n")
    with pytest.raises(apply.Refused, match="bundle_sha_mismatch"):
        apply.bundle(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["files"][0]["path"] = "/etc/../root/x"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(apply.Refused, match="bundle_path_invalid"):
        apply.bundle(tmp_path)
