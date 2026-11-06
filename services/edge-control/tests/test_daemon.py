import json
from types import SimpleNamespace

import pytest

from bravo_edge_control.daemon import healthy, serve
from bravo_edge_control.provider import EdgeError


class Stop:
    def __init__(self, cycles):
        self.cycles = cycles

    def is_set(self):
        return self.cycles == 0

    def wait(self, seconds):
        self.cycles -= 1


@pytest.mark.parametrize("terminal", ["RESTORED", "ABORTED", "STOPPED"])
def test_terminal_result_never_reexecutes_after_wait(terminal, tmp_path):
    calls = []
    def sequence(**kwargs):
        calls.append(True)
        return {"status": terminal}
    path = tmp_path / "health"
    serve(SimpleNamespace(sequence=sequence), stop=Stop(3), health_file=path)
    assert len(calls) == 1
    assert json.loads(path.read_text())["state"] == terminal
    assert healthy(path) is (terminal != "STOPPED")
    assert healthy(path, live=True)


@pytest.mark.parametrize("previous,count,state", [(None, 3, "WAITING"),
                                                  ({"status": "UNCERTAIN"}, 1, "STOPPED"),
                                                  ({"status": "CONFIRMED"}, 1, "STOPPED")])
def test_unavailable_authority_only_waits_if_no_operation_was_admitted(previous, count, state, tmp_path):
    calls = []
    def sequence(**kwargs):
        calls.append(True)
        raise EdgeError("current_authority_required")
    host = SimpleNamespace(_controller=lambda _: SimpleNamespace(current=lambda: previous))
    path = tmp_path / "health"
    serve(SimpleNamespace(sequence=sequence, host=host), stop=Stop(3), health_file=path)
    assert len(calls) == count
    assert json.loads(path.read_text())["state"] == state
    assert not healthy(path)


def test_unreadable_journal_does_not_retry_an_execution(tmp_path):
    calls = []
    def failure(**kwargs):
        calls.append(True)
        raise OSError("private")
    path = tmp_path / "health"
    serve(SimpleNamespace(sequence=failure), stop=Stop(3), health_file=path)
    assert len(calls) == 1
    assert json.loads(path.read_text())["state"] == "STOPPED"


@pytest.mark.parametrize("value", [{}, {"observed_at": 10, "state": "RUNNING"},
                                  {"observed_at": 111, "state": "RUNNING"}])
def test_missing_stale_future_health_never_ready(value, tmp_path):
    path = tmp_path / "health"
    path.write_text(json.dumps(value))
    assert not healthy(path, clock=lambda: 110)
