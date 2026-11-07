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
    with pytest.raises(apply.Refused, match="bundle_path_not_allowed"):
        apply.bundle(tmp_path)


NONE_POLICY = b"apiVersion: audit.k8s.io/v1\nkind: Policy\nmetadata: {}\nrules:\n- level: None\n"


def bundle_dir(tmp_path):
    root = tmp_path / "bundle"
    for name, content in files(state("platform")).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)
    return root


class Node:
    """A fake RKE2 server under a temporary root: kubectl, the API server restart and its audit log."""

    def __init__(self, apply, tmp_path, monkeypatch, *, uid="uid-1", ready=True, grows=True, crash=None):
        import os
        self.root = tmp_path / "root"
        self.policy = self.root / "etc/rancher/rke2/audit-policy.yaml"
        self.policy.parent.mkdir(parents=True)
        self.policy.write_bytes(NONE_POLICY)
        os.chmod(self.policy, 0o600)
        self.log = tmp_path / "audit.log"
        self.log.write_text("")
        manifest = tmp_path / "kube-apiserver.yaml"
        manifest.write_text(f"- --audit-policy-file=/etc/rancher/rke2/audit-policy.yaml\n- --audit-log-path={self.log}\n")
        self.uid, self.ready, self.grows, self.crash, self.restarts = uid, ready, grows, crash, 0
        monkeypatch.setattr(apply, "ROOT", self.root)
        monkeypatch.setattr(apply, "APISERVER_MANIFEST", manifest)
        monkeypatch.setattr(apply, "kubectl", self.kubectl)
        monkeypatch.setattr(apply, "restart_apiserver", self.restart)
        monkeypatch.setattr(apply.time, "sleep", lambda seconds: None)
        self.apply = apply

    def kubectl(self, *args):
        return json.dumps({"metadata": {"uid": self.uid}}) if args[-1] == "json" else "namespace/kube-system"

    def restart(self):
        self.restarts += 1
        if self.restarts == 1 and self.crash is not None:
            raise self.crash
        if self.restarts == 1 and not self.ready:
            raise self.apply.Refused("api_server_not_ready")
        if self.grows:
            self.log.write_text(self.log.read_text() + "event\n")
        return f"container-{self.restarts}"


def reviewed():
    """The bytes a reviewer saw, as GET .../node-files lists them."""
    return [f"{r['path']}={r['sha256']}" for r in listing(state("platform"))["files"]]


def run_applier(apply, bundle, capsys, *flags, sha=True):
    pairs = [x for pair in reviewed() for x in ("--sha256", pair)] if sha and "--apply" in flags else []
    code = apply.main([str(bundle), "--cluster-uid", "uid-1", *flags, *pairs])
    return code, json.loads(capsys.readouterr().out)


def test_the_applier_allows_exactly_what_the_node_modules_render():
    apply = applier()
    rows = [r for env in ("platform", "target-dev", "t49") for r in listing(state(env))["files"]]
    assert {r["path"]: {"mode": r["mode"], "restart": r["restart"]} for r in rows} == apply.ALLOWED


def test_the_applier_refuses_paths_modes_restarts_and_states_it_was_not_given(tmp_path):
    apply = applier()
    bundle = bundle_dir(tmp_path)
    assert apply.bundle(bundle, listing(state("platform"))["state_digest"])
    with pytest.raises(apply.Refused, match="bundle_state_digest_mismatch"):
        apply.bundle(bundle, "0" * 64)
    manifest = json.loads((bundle / "manifest.json").read_text())
    for change, code in ((("path", "/etc/sudoers.d/x"), "bundle_path_not_allowed"),
                         (("restart", []), "bundle_mode_or_restart_not_allowed"),
                         (("mode", "0644"), "bundle_mode_or_restart_not_allowed")):
        edited = json.loads(json.dumps(manifest))
        edited["files"][0][change[0]] = change[1]
        (bundle / "manifest.json").write_text(json.dumps(edited))
        with pytest.raises(apply.Refused, match=code):
            apply.bundle(bundle)


def test_identity_is_checked_before_any_write(tmp_path, monkeypatch, capsys):
    apply = applier()
    node = Node(apply, tmp_path, monkeypatch, uid="another-cluster")
    code, report = run_applier(apply, bundle_dir(tmp_path), capsys, "--apply")
    assert code == 1 and report["reason"] == "cluster_identity_mismatch"
    assert node.policy.read_bytes() == NONE_POLICY and node.restarts == 0


def test_without_apply_nothing_is_written(tmp_path, monkeypatch, capsys):
    apply = applier()
    node = Node(apply, tmp_path, monkeypatch)
    code, report = run_applier(apply, bundle_dir(tmp_path), capsys)
    assert code == 0 and report["files"][0]["action"] == "install" and not report["applied"]
    assert node.policy.read_bytes() == NONE_POLICY and node.restarts == 0


def test_apply_backs_up_installs_restarts_and_sees_the_audit_log_grow(tmp_path, monkeypatch, capsys):
    import stat
    apply = applier()
    node = Node(apply, tmp_path, monkeypatch)
    code, report = run_applier(apply, bundle_dir(tmp_path), capsys, "--apply")
    assert code == 0 and report["status"] == "PASS", report
    assert node.policy.read_bytes() == CANONICAL.read_bytes()
    assert stat.S_IMODE(node.policy.stat().st_mode) == 0o600
    backup = Path(report["files"][0]["backup"])
    assert backup.read_bytes() == NONE_POLICY and report["audit_log"]["growing"] is True
    assert report["restarted"] == [{"target": "StaticPod/kube-system/kube-apiserver", "container": "container-1"}]


@pytest.mark.parametrize("failure, reason", [
    ({"ready": False}, "api_server_not_ready"),
    ({"crash": __import__("subprocess").CalledProcessError(1, "crictl")}, "CalledProcessError"),
    ({"grows": False}, "audit_log_not_growing"),
])
def test_a_failure_after_install_puts_the_previous_policy_back(tmp_path, monkeypatch, capsys, failure, reason):
    apply = applier()
    node = Node(apply, tmp_path, monkeypatch, **failure)
    code, report = run_applier(apply, bundle_dir(tmp_path), capsys, "--apply")
    assert code == 1 and report["reason"] == reason, report
    assert report["rolled_back"] is True and report["rollback_ready"] is True
    assert node.policy.read_bytes() == NONE_POLICY


def test_a_crashed_container_fails_fast_instead_of_waiting_out_the_timeout(monkeypatch):
    apply = applier()
    since = 1_000
    listing_json = {"containers": [
        {"id": "old", "state": "CONTAINER_EXITED", "createdAt": "900"},       # exited before: not ours
        {"id": "new", "state": "CONTAINER_EXITED", "createdAt": "1100"}]}     # ours, already exited
    calls = []

    def fake_run(cmd, capture_output=True, text=False, check=False):
        calls.append(cmd)
        return __import__("types").SimpleNamespace(stdout=json.dumps(listing_json), returncode=0)

    monkeypatch.setattr(apply.subprocess, "run", fake_run)
    monkeypatch.setattr(apply.time, "sleep", lambda seconds: None)
    assert apply.crashed_since(since, "old") is True
    listing_json["containers"].pop()
    assert apply.crashed_since(since, "old") is False
    listing_json["containers"].append({"id": "new", "state": "CONTAINER_EXITED", "createdAt": "1100"})
    with pytest.raises(apply.Refused, match="api_server_crashed"):
        apply.wait_ready("old", since)


def test_writing_as_root_needs_the_reviewed_bytes_not_the_bundles_own_word(tmp_path, monkeypatch, capsys):
    apply = applier()
    node = Node(apply, tmp_path, monkeypatch)
    code, report = run_applier(apply, bundle_dir(tmp_path), capsys, "--apply", sha=False)
    assert code == 1 and report["reason"] == "reviewed_sha256_required" and node.restarts == 0
    # Someone who can edit the bundle turns the policy off, fixes its manifest sha and copies the state_digest.
    bundle = bundle_dir(tmp_path)
    (bundle / "etc/rancher/rke2/audit-policy.yaml").write_bytes(NONE_POLICY)
    manifest = json.loads((bundle / "manifest.json").read_text())
    manifest["files"][0]["sha256"] = apply.sha(NONE_POLICY)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    digest = listing(state("platform"))["state_digest"]
    code, report = run_applier(apply, bundle, capsys, "--state-digest", digest, "--apply")
    assert code == 1 and report["reason"] == "bundle_not_reviewed_bytes"
    assert node.policy.read_bytes() == NONE_POLICY and node.restarts == 0


def test_a_rollback_that_cannot_write_says_which_file_kept_the_new_bytes(tmp_path, monkeypatch, capsys):
    apply = applier()
    node = Node(apply, tmp_path, monkeypatch, ready=False)
    real = apply.install

    def install(path, data, mode):
        if path == node.policy and data == NONE_POLICY:  # putting the previous policy back fails
            raise OSError("read-only file system")
        real(path, data, mode)

    monkeypatch.setattr(apply, "install", install)
    code, report = run_applier(apply, bundle_dir(tmp_path), capsys, "--apply")
    assert code == 1 and report["reason"] == "api_server_not_ready"
    assert report["rolled_back"] is False and report["rollback_failed"] == [str(node.policy)]
    assert "rollback_ready" in report  # the API server state is still reported


def test_a_rotated_audit_log_with_new_events_counts_as_growing(tmp_path, monkeypatch, capsys):
    apply = applier()
    node = Node(apply, tmp_path, monkeypatch, grows=False)
    node.log.write_text("old events\n" * 50)

    def restart():
        node.restarts += 1
        node.log.rename(node.log.with_suffix(".1"))  # rotated: a new, shorter file
        node.log.write_text("event\n")
        return "container-1"

    monkeypatch.setattr(apply, "restart_apiserver", restart)
    code, report = run_applier(apply, bundle_dir(tmp_path), capsys, "--apply")
    assert code == 0 and report["audit_log"]["growing"] is True, report


ISSUER = "https://bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/t49"
IN_CLUSTER = "https://kubernetes.default.svc.cluster.local"


def test_the_issuer_module_renders_a_drop_in_and_refuses_other_issuers():
    [row] = listing(state("t49"))["files"]
    assert row["path"] == "/etc/rancher/rke2/config.yaml.d/50-bravo-service-account-issuer.yaml"
    assert row["restart"] == ["Service/rke2-server"] and row["mode"] == "0600"
    content = files(state("t49"))["etc/rancher/rke2/config.yaml.d/50-bravo-service-account-issuer.yaml"]
    flags = [line.strip()[2:] for line in content.splitlines() if line.strip().startswith("- ")]
    assert flags == [f"service-account-issuer={ISSUER}", f"service-account-issuer={IN_CLUSTER}",
                     f"service-account-jwks-uri={ISSUER}/openid/v1/jwks"]  # the public one first: it signs
    assert content.splitlines()[1] == "kube-apiserver-arg+:"  # appends; never replaces config.yaml's own args
    for bad in ("http://bravo-oidc-issuer.example/t49", ISSUER + "/", ISSUER + "\n  - x=y", ISSUER + "?a=b", ""):
        broken = json.loads(json.dumps(state("t49")))
        broken["modules"][-1]["params"]["issuer"] = bad
        with pytest.raises(IacError):
            listing(broken)


class IssuerNode:
    """A fake RKE2 server: restarting rke2-server renders the drop-in's flags into the API server."""

    def __init__(self, apply, tmp_path, monkeypatch, *, takes_effect=True):
        self.root, self.manifest = tmp_path / "root", tmp_path / "kube-apiserver.yaml"
        self.root.mkdir()
        self.dropin = self.root / "etc/rancher/rke2/config.yaml.d/50-bravo-service-account-issuer.yaml"
        self.served, self.restarts, self.takes_effect = IN_CLUSTER, [], takes_effect
        self.manifest.write_text(f"--service-account-issuer={IN_CLUSTER}\n")
        monkeypatch.setattr(apply, "ROOT", self.root)
        monkeypatch.setattr(apply, "APISERVER_MANIFEST", self.manifest)
        monkeypatch.setattr(apply, "kubectl", self.kubectl)
        monkeypatch.setattr(apply, "restart_rke2_server", self.restart)

    def kubectl(self, *args):
        if args[:2] == ("get", "ns"):
            return json.dumps({"metadata": {"uid": "uid-1"}})
        return json.dumps({"issuer": self.served})

    def restart(self):
        self.restarts.append("rke2-server")
        if self.dropin.exists():
            flags = [line.strip()[2:] for line in self.dropin.read_text().splitlines() if line.strip().startswith("- ")]
            self.manifest.write_text("".join(f"--{flag}\n" for flag in flags))
            self.served = ISSUER if self.takes_effect else IN_CLUSTER
        else:
            self.manifest.write_text(f"--service-account-issuer={IN_CLUSTER}\n")
            self.served = IN_CLUSTER
        return f"apiserver-{len(self.restarts)}"


def issuer_bundle(tmp_path):
    root = tmp_path / "bundle"
    for name, content in files(state("t49")).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)
    return root, [x for r in listing(state("t49"))["files"] for x in ("--sha256", f"{r['path']}={r['sha256']}")]


def test_the_issuer_drop_in_restarts_rke2_server_and_must_take_effect(tmp_path, monkeypatch, capsys):
    apply = applier()
    node = IssuerNode(apply, tmp_path, monkeypatch)
    bundle, pairs = issuer_bundle(tmp_path)
    assert apply.main([str(bundle), "--cluster-uid", "uid-1", "--apply", *pairs]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "PASS" and report["restarted"] == [{"target": "Service/rke2-server",
                                                                    "container": "apiserver-1"}]
    assert node.dropin.exists() and oct(node.dropin.stat().st_mode & 0o777) == "0o600" and node.served == ISSUER
    assert apply.main([str(bundle), "--cluster-uid", "uid-1", "--apply", *pairs]) == 0  # same bytes: no restart
    assert json.loads(capsys.readouterr().out)["restarted"] == [] and node.restarts == ["rke2-server"]


def test_an_issuer_that_does_not_take_effect_is_rolled_back(tmp_path, monkeypatch, capsys):
    apply = applier()
    node = IssuerNode(apply, tmp_path, monkeypatch, takes_effect=False)
    bundle, pairs = issuer_bundle(tmp_path)
    assert apply.main([str(bundle), "--cluster-uid", "uid-1", "--apply", *pairs]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason"] == "service_account_issuer_not_in_effect"
    assert report["rolled_back"] and report["rollback_ready"] and not node.dropin.exists()
    assert node.restarts == ["rke2-server", "rke2-server"] and node.served == IN_CLUSTER
