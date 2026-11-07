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
        if argv[-2:] == ["sh", "-c"] or (len(argv) > 2 and argv[-3:-1] == ["sh", "-c"]):
            fake.sent.append(kw.get("stdin"))
        return ""

    monkeypatch.setattr(drill, "kubectl", kubectl)
    monkeypatch.setattr(drill, "proof", lambda: "proof")
    monkeypatch.setattr(drill.time, "sleep", lambda s: None)
    fake = SimpleNamespace(calls=calls, streams=[], sent=[])

    def stream(a, pod, remote, action, stop=None):
        fake.streams.append(action)
        return fake.results.pop(0)

    monkeypatch.setattr(drill, "stream", stream)
    return fake


SPEC = {"run_id": "drill-a", "plan_id": "plan-1", "approval_id": "appr-1"}


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


def test_unapproved_specs_do_not_run_and_only_host_keys_are_sent(tmp_path, fake):
    fake.results = []
    with pytest.raises(SystemExit, match="approve the spec first"):
        drill.run(args(tmp_path, SPEC | {"approval_id": "PENDING"}))
    assert fake.streams == []
    fake.results = [(0, False)]
    with pytest.raises(SystemExit):
        drill.run(args(tmp_path, SPEC | {"approved_by": "x"}, action="register"))  # a spec from the earlier drill.py
    assert "approved_by" not in json.loads(fake.sent[-1])  # load_drill refuses any key it does not know


def test_a_run_id_that_could_reach_the_shell_is_refused(tmp_path, fake):
    with pytest.raises(SystemExit, match="run_id must match"):
        drill.run(args(tmp_path, SPEC | {"run_id": "x; rm -rf /"}))


def test_another_cluster_stops_before_anything(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(drill, "kubectl", lambda argv, **kw: "uid-2")
    with pytest.raises(SystemExit, match="cluster identity mismatch"):
        drill.run(args(tmp_path, SPEC))


def test_what_put_sends_passes_the_hosts_own_key_check(tmp_path, fake):
    """Run from services/edge-control (`uv run pytest ../../operations/edge-drill`) to use the real load_drill."""
    host_drill = pytest.importorskip("bravo_edge_control.drill")
    config = {"version": 1, "plan_id": "plan-1", "run_id": "drill-a", "approval_id": "appr-1", "profile": {},
              "lb_binding": {}, "journal": str(tmp_path / "missing" / "edge.sqlite"),
              "token_file": str(tmp_path / "token"), "operator_host": "x", "approved_by": "earlier drill.py"}
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(config))
    drill.put(SimpleNamespace(kubeconfig="kc"), "host-0", str(path))
    with pytest.raises(ValueError) as refused:
        host_drill.load_drill(None, json.loads(fake.sent[-1]))
    assert str(refused.value) == "persistent_journal_mount_required"  # past the exact-keys check
