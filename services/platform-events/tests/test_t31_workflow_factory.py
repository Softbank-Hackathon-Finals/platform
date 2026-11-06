"""T31's `application` factory (bravo_apply.workflow_factory) as the workflow host loads it.

No target kubeconfig mounted: REHEARSE holds with a named reason and the host keeps running. A
kubeconfig mounted later is picked up on the next sweep without a restart.
"""

import json
import os
import sys
import types

import pytest
from bravo_apply import workflow_factory
from test_workflow_ports import mounts  # noqa: F401  (fixture)
from test_workflow_recovery import Producers, collect, service

from bravo_platform_events import workflow_ports
from bravo_platform_events.application_host import ApplicationHost
from bravo_platform_events.change import ChangeApprovalService
from bravo_platform_events.store import EventError
from bravo_platform_events.workflow import WorkflowRecovery
from bravo_platform_events.workflow_host import StageReaders, WorkflowHost

SPEC = "bravo_apply.workflow_factory:application"
KUBECONFIG = {"apiVersion": "v1", "kind": "Config", "current-context": "apply",
              "clusters": [{"name": "t", "cluster": {"server": "https://target.example.invalid"}}],
              "users": [{"name": "u", "user": {"token": "fixture-not-a-token"}}],
              "contexts": [{"name": "apply", "context": {"cluster": "t", "user": "u"}}]}


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    calls, made = [], []
    module = types.ModuleType("t31_fixture_ports")

    def trusted(store):
        calls.append(store)
        return {"candidate": lambda *a: pytest.fail("no candidate without an approval"),
                "check_current": lambda *a: True}
    module.trusted = trusted
    monkeypatch.setitem(sys.modules, module.__name__, module)

    class Recorded(workflow_factory.KubectlKube):
        def __init__(self, path):
            made.append(path)
            super().__init__(path)
    monkeypatch.setattr(workflow_factory, "KubectlKube", Recorded)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kubectl").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "kubectl").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])  # git stays reachable
    (tmp_path / "pvc").mkdir()
    config = {"version": 1, "kubeconfig_file": str(tmp_path / "secret" / "kubeconfig"),
              "receipts_dir": str(tmp_path / "pvc" / "applications"),
              "target": {"environment_id": "env-target", "namespace": "demo-chat", "deployment": "demo-chat",
                         "container": "app", "repository": "ghcr.io/example/demo-chat"},
              "host_subject": "workflow-host", "trusted_ports": "t31_fixture_ports:trusted"}
    (tmp_path / "apply.json").write_text(json.dumps(config))
    monkeypatch.setenv(workflow_factory.CONFIG_ENV, str(tmp_path / "apply.json"))
    return tmp_path, calls, made, module


def mount_kubeconfig(root):
    (root / "secret").mkdir()
    (root / "secret" / "kubeconfig").write_text(json.dumps(KUBECONFIG))


def test_no_kubeconfig_holds_rehearse_by_name_then_a_mounted_one_is_used(backend, tmp_path, deployment):
    root, calls, made, _ = deployment
    store = backend.store
    stages = workflow_ports._factory(SPEC)(store)  # the loader create_ports uses for "application"
    assert set(stages) == {"REHEARSE", "GATE"} and calls == [store]
    producers = Producers(tmp_path / "remote.db")
    host = WorkflowHost(WorkflowRecovery(service(store, None), read_evidence=StageReaders(
        {"ANALYZE": producers.read, "PROPOSE": producers.read}, preparers=stages)))
    collect(store)
    [step] = host.sweep()
    assert (step.after, step.hold) == ("REHEARSE", "t31_target_kubeconfig_unavailable")
    assert made == [] and not (root / "pvc" / "applications").exists()
    [step] = host.sweep()  # still holding, still no adapter; the host loop keeps running
    assert step.hold == "t31_target_kubeconfig_unavailable" and made == []

    mount_kubeconfig(root)  # e.g. the optional Secret is created later
    [step] = host.sweep()
    # Built now: one adapter on the mounted file, the receipt lock is held, and REHEARSE waits on
    # the human S2 approval (no approval was given, so nothing is claimed or applied).
    assert (step.after, step.hold) == ("REHEARSE", "change_approval_required")
    assert made == [str(root / "secret" / "kubeconfig")]
    assert (root / "pvc" / "applications" / ".host.lock").exists()
    changes = ChangeApprovalService(store, authority=lambda *a, **k: None, check_access=lambda *a: None)
    with pytest.raises(BlockingIOError):  # a second host on the same receipts is refused
        ApplicationHost(changes, kube=None, receipts_dir=root / "pvc" / "applications", load_input=print)
    host.sweep()
    assert len(made) == 1  # built once per process
    with pytest.raises(EventError, match="workflow_reader_unwired"):
        stages["GATE"]("plan", "tenant", 1)  # no T19 ports configured: GATE stays unwired


def test_missing_receipt_mount_and_kubectl_are_holds_too(backend, deployment, monkeypatch):
    root, _, made, _ = deployment
    mount_kubeconfig(root)
    stages = workflow_factory.application(backend.store)
    (root / "pvc").rmdir()
    with pytest.raises(EventError, match="t31_receipts_mount_unavailable"):
        stages["REHEARSE"]("plan", "tenant", 1)
    monkeypatch.setenv("PATH", str(root / "nothing"))
    with pytest.raises(EventError, match="t31_kubectl_unavailable"):
        stages["REHEARSE"]("plan", "tenant", 1)
    assert not (root / "pvc").exists()


@pytest.mark.parametrize("ports", [None, {}, {"candidate": print}, {"candidate": print, "check_current": 1},
                                   {"candidate": print, "check_current": print, "extra": print},
                                   {"candidate": print, "check_current": print, "gate": {"records": 1}}])
def test_invalid_trusted_ports_fail_startup(backend, deployment, monkeypatch, ports):
    monkeypatch.setattr(deployment[3], "trusted", lambda store: ports)
    with pytest.raises(ValueError, match="t31_trusted_ports_invalid"):
        workflow_factory.application(backend.store)


def test_missing_apply_configuration_fails_startup(backend, deployment, monkeypatch):
    monkeypatch.setenv(workflow_factory.CONFIG_ENV, str(deployment[0] / "absent.json"))
    with pytest.raises(ValueError, match="t31_apply_configuration_unavailable"):
        workflow_factory.application(backend.store)


def test_create_ports_wires_the_t31_factory(backend, mounts, deployment):  # noqa: F811
    config, write, _ = mounts
    config["application"] = SPEC
    ports = workflow_ports.create_ports(backend.store, write(config))
    assert {"PROPOSE", "REHEARSE", "GATE"} <= set(ports["preparers"])
