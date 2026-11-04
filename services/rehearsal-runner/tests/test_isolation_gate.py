from types import SimpleNamespace

import pytest

from rehearsal_runner import isolation_gate as gate
from rehearsal_runner.isolation.model import Kind, Result, Status


class Kube:
    def __init__(self):
        self.uids = iter(["uid-1", "uid-1", "uid-1"])
        self.items = []
        self.fail = False

    def get_json(self, *args):
        if self.fail:
            raise RuntimeError("unavailable")
        if args[0] == "namespace":
            return {"metadata": {"uid": next(self.uids)}}
        return {"items": self.items}

    def run(self, *args, **kwargs):
        return SimpleNamespace(returncode=0)


@pytest.fixture
def passed(monkeypatch):
    monkeypatch.setattr(gate, "run", lambda *_: [Result("N", "test", Kind.ISOLATION, Status.PASS)])


def test_pass_requires_identity_and_cleanup(passed):
    result = gate.check_isolation(Kube(), "rehearsal-test", [], "image")
    assert result.namespace_uid == "uid-1"
    assert result.namespace == "rehearsal-test"


@pytest.mark.parametrize("status", [Status.FAIL, Status.INCONCLUSIVE])
def test_failed_or_unknown_is_blocked(monkeypatch, status):
    monkeypatch.setattr(gate, "run", lambda *_: [Result("N", "test", Kind.ISOLATION, status)])
    with pytest.raises(gate.IsolationBlocked):
        gate.check_isolation(Kube(), "rehearsal-test", [], "image")


def test_recreated_namespace_is_blocked(passed):
    kube = Kube()
    kube.uids = iter(["uid-1", "uid-2"])
    with pytest.raises(gate.IsolationBlocked):
        gate.check_isolation(kube, "rehearsal-test", [], "image")


def test_remaining_fixtures_block(passed):
    kube = Kube()
    kube.items = [{"metadata": {"name": "remaining"}}]
    with pytest.raises(gate.IsolationBlocked):
        gate.check_isolation(kube, "rehearsal-test", [], "image", cleanup_timeout=0)


def test_unavailable_cluster_blocks(passed):
    kube = Kube()
    kube.fail = True
    with pytest.raises(gate.IsolationBlocked):
        gate.check_isolation(kube, "rehearsal-test", [], "image")


def test_cleanup_runs_on_probe_exception(monkeypatch):
    calls = []
    def fail(*_):
        raise RuntimeError("probe unavailable")
    monkeypatch.setattr(gate, "run", fail)
    monkeypatch.setattr(gate.Fixtures, "cleanup", lambda *_: calls.append("cleanup"))
    with pytest.raises(gate.IsolationBlocked):
        gate.check_isolation(Kube(), "rehearsal-test", [], "image")
    assert calls == ["cleanup"]


def test_missing_isolation_results_block(monkeypatch):
    monkeypatch.setattr(gate, "run", lambda *_: [Result("P", "control", Kind.CONTROL, Status.PASS)])
    with pytest.raises(gate.IsolationBlocked):
        gate.check_isolation(Kube(), "rehearsal-test", [], "image")


def test_cleanup_waits_for_async_deletion(passed, monkeypatch):
    kube = Kube()
    calls = iter([False, True])
    monkeypatch.setattr(gate, "_fixtures_absent", lambda *_: next(calls))
    monkeypatch.setattr(gate.time, "sleep", lambda _: None)
    assert gate.check_isolation(kube, "rehearsal-test", [], "image").namespace_uid == "uid-1"


def test_identity_change_during_cleanup_blocks(passed):
    kube = Kube()
    kube.uids = iter(["uid-1", "uid-1", "uid-2"])
    with pytest.raises(gate.IsolationBlocked):
        gate.check_isolation(kube, "rehearsal-test", [], "image")


def test_cli_blocks_without_proceeding(monkeypatch, tmp_path, capsys):
    from rehearsal_runner import isolation_cli

    profile = tmp_path / "profile.env"
    profile.write_text("bad profile")
    assert isolation_cli.main(["--namespace", "test", "--profile", str(profile)]) == 1
    assert '"BLOCK"' in capsys.readouterr().out
