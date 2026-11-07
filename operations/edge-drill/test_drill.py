"""Local checks for drill.py (no cluster): `python3 -m pytest operations/edge-drill/test_drill.py`."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import drill


def args(tmp_path, spec, action="sequence"):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return SimpleNamespace(kubeconfig="kc", cluster_uid="uid-1", spec=str(path), action=action)


@pytest.fixture
def fake(monkeypatch):
    calls = []

    def kubectl(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["get", "ns"]:
            return "uid-1"
        if "jsonpath={.items[0].metadata.name}" in argv:
            return "host-0"
        if drill.WHOAMI in argv:
            return json.dumps({"subject_id": fake.me})
        return ""

    monkeypatch.setattr(drill, "kubectl", kubectl)
    monkeypatch.setattr(drill, "proof", lambda: "proof")
    monkeypatch.setattr(drill.time, "sleep", lambda s: None)
    fake = SimpleNamespace(calls=calls, me="runner", streams=[])

    def stream(a, pod, remote, action, stop=None):
        fake.streams.append(action)
        return fake.results.pop(0)

    monkeypatch.setattr(drill, "stream", stream)
    return fake


SPEC = {"run_id": "drill-a", "plan_id": "plan-1", "approval_id": "appr-1", "approved_by": "approver"}


def test_a_failed_sequence_stops_the_remote_drill_and_restores_to_zero(tmp_path, fake):
    fake.results = [(2, False), (1, False), (0, False)]  # sequence fails; restore refused once (lock), then ok
    with pytest.raises(SystemExit) as end:
        drill.run(args(tmp_path, SPEC))
    assert fake.streams == ["sequence", "restore", "restore"] and end.value.code == 2
    assert any(drill.STOP in c for c in fake.calls)


def test_an_interrupted_sequence_restores_and_exits_130(tmp_path, fake):
    fake.results = [(0, True), (0, False)]
    with pytest.raises(SystemExit) as end:
        drill.run(args(tmp_path, SPEC))
    assert fake.streams == ["sequence", "restore"] and end.value.code == 130


def test_the_approver_cannot_run_and_unapproved_specs_do_not_run(tmp_path, fake):
    fake.me, fake.results = "approver", []
    with pytest.raises(SystemExit, match="approver cannot run"):
        drill.run(args(tmp_path, SPEC))
    with pytest.raises(SystemExit, match="approve the spec first"):
        drill.run(args(tmp_path, SPEC | {"approval_id": "PENDING"}))
    assert fake.streams == []


def test_a_run_id_that_could_reach_the_shell_is_refused(tmp_path, fake):
    with pytest.raises(SystemExit, match="run_id must match"):
        drill.run(args(tmp_path, SPEC | {"run_id": "x; rm -rf /"}))


def test_another_cluster_stops_before_anything(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(drill, "kubectl", lambda argv, **kw: "uid-2")
    with pytest.raises(SystemExit, match="cluster identity mismatch"):
        drill.run(args(tmp_path, SPEC))
