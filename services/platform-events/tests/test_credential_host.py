"""Host composition with real stored plan/admission; Kubernetes effects are synthetic."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_initial_proposal import ACTOR, RUNTIME
from test_initial_rehearsal import flow as flow

from bravo_platform_events import credential_host as host


@pytest.fixture
def configured(flow, tmp_path, monkeypatch):
    flow.ready()
    flow.start()
    admission, = flow.effects
    with flow.f.backend.store._transaction() as c:
        plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (admission.plan_id,)).fetchone())
        task = replace(flow.f.catalog.for_plan(c, plan, admission.task_id), run_id=admission.rehearsal_id)
    root = Path(__file__).resolve().parents[3]
    monkeypatch.setattr(host, "IMAGE_MODULE_ROOT", root / "infra/envs")
    config = dict(reader_config=str(tmp_path / "reader.json"), runtime_source_sha=RUNTIME,
        target_environment_id="env-onprem", cluster_uid="synthetic-cluster",
        manager_subject="system:serviceaccount:bravo-rehearsal-control:fixture-manager",
        manager_kubeconfig=str(tmp_path / "manager.json"), runner_kubeconfig=str(tmp_path / "runner.json"),
        profile_file=str(tmp_path / "profile.env"), isolation_image="fixture/probe@sha256:" + "a" * 64,
        t9_renderer=str(root / "infra/envs/target-secrets/render.py"),
        scoped_lifecycle_module=str(root / "infra/envs/rehearsal-access/scoped_lifecycle.py"),
        candidate_repository="fixture/candidate", staging=str(tmp_path),
        fixture=dict(bao_image="fixture/bao@sha256:" + "b" * 64,
            postgres_image="fixture/postgres@sha256:" + "c" * 64,
            probe_image="fixture/probe@sha256:" + "d" * 64, openbao_ca_file=str(tmp_path / "ca.pem")))
    for name in ("reader_config", "manager_kubeconfig", "runner_kubeconfig"):
        Path(config[name]).write_text('{}')
    Path(config["fixture"]["openbao_ca_file"]).write_text("-----BEGIN CERTIFICATE-----\nfixture\n")
    Path(config["profile_file"]).write_text("\n".join(
        f"{name}_{key}={value}" for name in ("SEAWEEDFS", "OPENBAO")
        for key, value in (("NAMESPACE", "target"), ("LABEL_KEY", "app"), ("LABEL_VALUE", name.lower()),
                           ("PORT", "8200"))))
    binding = flow.f.execution().rehearsal_binding(admission)
    config["target_environment_id"] = binding.target_environment_id
    reader = SimpleNamespace(records=flow.f.reopen(), caller=SimpleNamespace(principal=ACTOR))
    monkeypatch.setattr(host, "configured_initial_reader", lambda *a: reader)
    clients = []
    def make_clients(path, identity):
        kube = SimpleNamespace(get=lambda *a: {"metadata": {"uid": "synthetic-cluster"}},
            call=lambda *a: json.dumps({"clusters": [{"cluster": {"server": "https://target.invalid"}}]}))
        kubectl = SimpleNamespace(identity=identity)
        clients.append((path, identity, kube, kubectl))
        return kube, kubectl
    monkeypatch.setattr(host, "_clients", make_clients)
    file = tmp_path / "ports.json"
    file.write_text(json.dumps(config))
    return flow, file, config, admission, task, clients


def test_composes_current_source_and_separate_clients(configured):
    flow, path, config, admission, task, clients = configured
    ports = host.create_ports(flow.f.backend.store, path)
    assert [c[1] for c in clients] == [config["manager_subject"], host.RUNNER]
    factory = ports.planner.factory(admission, task)
    planner = factory.inner.planner
    assert planner.kube is clients[1][2] and planner.kubectl is clients[1][3]
    assert factory.provisioner is clients[0][3] and factory.cleaner is clients[0][2]
    assert planner.candidate_image(flow.f.published["diff_hash"]) == (
        config["candidate_repository"] + "@" + flow.f.published["image_digest"])
    coordinator, provider = planner.proposals.open(admission, task)
    assert coordinator.snapshot.read("app/db_secret.py") == flow.f.source_value["files"]["app/db_secret.py"]
    assert provider.result == flow.f.execution().rehearsal_result(admission)
    assert len(flow.f.client.submits) == 1 and not flow.f.client.reads


@pytest.mark.parametrize("fault", ["same-client", "same-subject", "wrong-target", "wrong-cluster",
                                   "relative", "missing", "extra", "mutable-runtime"])
def test_unconfirmed_deployment_fails_closed(configured, fault):
    flow, path, config, admission, task, clients = configured
    if fault == "same-client":
        config["runner_kubeconfig"] = config["manager_kubeconfig"]
    elif fault == "same-subject":
        config["manager_subject"] = host.RUNNER
    elif fault == "wrong-target":
        config["target_environment_id"] = "other-target"
    elif fault == "wrong-cluster":
        config["cluster_uid"] = "other-cluster"
    elif fault == "relative":
        config["staging"] = "relative"
    elif fault == "missing":
        config["profile_file"] += ".missing"
    elif fault == "extra":
        config["grant"] = True
    else:
        config["runtime_source_sha"] = "develop"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        host.create_ports(flow.f.backend.store, path).planner.factory(admission, task)
    assert len(flow.f.client.submits) == 1 and not flow.f.client.reads


def test_stored_host_has_no_provider_capability():
    client = host._StoredOnly()
    for name in ("recipient", "submit", "read", "validate"):
        with pytest.raises(RuntimeError, match="cannot_dispatch"):
            getattr(client, name)(object())


@pytest.mark.parametrize("name", ["t9_renderer", "scoped_lifecycle_module"])
@pytest.mark.parametrize("symlink", [False, True])
def test_unreviewed_module_is_never_loaded(configured, tmp_path, name, symlink):
    flow, path, config, *_ = configured
    unreviewed = tmp_path / "unreviewed.py"
    unreviewed.write_text("raise AssertionError('unreviewed_module_executed')\n")
    selected = tmp_path / "module.py"
    if symlink:
        selected.symlink_to(unreviewed)
    else:
        selected.write_bytes(unreviewed.read_bytes())
    config[name] = str(selected)
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="image_modules_required"):
        host.create_ports(flow.f.backend.store, path)


def test_runner_cannot_select_another_cluster(configured, monkeypatch):
    flow, path, *_ = configured
    original = host._clients
    def clients(config, identity):
        kube, kubectl = original(config, identity)
        if identity == host.RUNNER:
            kube.call = lambda *a: json.dumps({"clusters": [{"cluster": {
                "server": "https://another-target.invalid"}}]})
        return kube, kubectl
    monkeypatch.setattr(host, "_clients", clients)
    with pytest.raises(ValueError, match="clients_target_mismatch"):
        host.create_ports(flow.f.backend.store, path)


@pytest.mark.parametrize("username", [host.RUNNER, "system:serviceaccount:other:runner", None])
def test_client_checks_actual_identity(monkeypatch, username):
    calls = []
    def call(self, *args, data=None):
        calls.append((self.path, args))
        return json.dumps({"status": {"userInfo": {"username": username}}})
    monkeypatch.setattr(host.ConfiguredKube, "call", call)
    if username == host.RUNNER:
        host._clients("/fixture/runner.json", host.RUNNER)
    else:
        with pytest.raises(ValueError, match="identity_unconfirmed"):
            host._clients("/fixture/runner.json", host.RUNNER)
    assert calls == [("/fixture/runner.json", ("auth", "whoami", "-o", "json"))]


@pytest.mark.parametrize("cluster", [
    {"server": "http://target.invalid"},
    {"server": "https://target.invalid", "insecure-skip-tls-verify": True},
    {"server": "https://target.invalid", "proxy-url": "http://other.invalid"},
    {"server": "https://user@target.invalid"},
])
def test_connection_requires_same_verified_tls_route(cluster):
    kube = SimpleNamespace(call=lambda *a: json.dumps({"clusters": [{"cluster": cluster}]}))
    with pytest.raises(ValueError, match="connection_unconfirmed"):
        host._connection(kube)
