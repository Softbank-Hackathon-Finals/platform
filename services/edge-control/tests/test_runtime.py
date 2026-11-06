"""Real T10/T36 control loop; synthetic provider and measurements only."""

from threading import Event

import pytest
from test_host import Fixture

from bravo_edge_control.provider import EdgeError
from bravo_edge_control.runtime import ShiftRuntime


def runtime(f):
    return ShiftRuntime(f.host, principal=lambda: "authenticated-fixture", plan_id="plan-host-fixture")


def test_complete_sequence_and_restart_do_not_repeat_effects(tmp_path):
    f = Fixture(tmp_path)
    seen = []

    def receive(value):
        if "receipt" in value:
            seen.append(value["receipt"]["weight_target"])
            if value["receipt"]["weight_target"]:
                f.window(value["receipt"]["weight_target"])

    result = runtime(f).sequence(stop=Event(), emit=receive)
    assert result["status"] == "RESTORED"
    assert seen == [10, 50, 100, 0]
    assert len(f.upstream.patches) == 4
    f.host = f.reopen()
    assert runtime(f).sequence(stop=Event())["status"] == "RESTORED"
    assert len(f.upstream.patches) == 4


def test_failure_window_automatically_restores_and_stays_aborted_on_restart(tmp_path):
    f = Fixture(tmp_path)

    def receive(value):
        if "receipt" in value:
            f.window(value["receipt"]["weight_target"], fail=True)

    result = runtime(f).sequence(stop=Event(), emit=receive)
    assert result["status"] == "ABORTED"
    assert result["rollback"]["weight_target"] == 0
    assert result["rollback"]["status"] == "CONFIRMED"
    assert len(f.upstream.patches) == 2
    f.host = f.reopen()
    assert runtime(f).sequence(stop=Event())["status"] == "ABORTED"
    assert len(f.upstream.patches) == 2


def test_second_runtime_cannot_start_or_observe_same_journal(tmp_path):
    f = Fixture(tmp_path)
    first, second = runtime(f), runtime(f)
    with first._owner(), pytest.raises(EdgeError, match="already_running"):
        second.sequence(stop=Event())
    assert not f.upstream.patches


def test_loop_rechecks_current_authority_before_advance(tmp_path):
    f = Fixture(tmp_path)

    def receive(value):
        if "receipt" in value:
            f.window(10)
            f.allowed = False

    with pytest.raises(EdgeError, match="authority_revoked"):
        runtime(f).sequence(stop=Event(), emit=receive)
    assert len(f.upstream.patches) == 1


def test_interrupted_run_resumes_same_stage_without_another_start(tmp_path):
    f = Fixture(tmp_path)
    stop = Event()

    def interrupt(value):
        if "receipt" in value:
            stop.set()

    assert runtime(f).sequence(stop=stop, emit=interrupt)["status"] == "STOPPED"
    assert len(f.upstream.patches) == 1
    f.host = f.reopen()

    def receive(value):
        if "receipt" in value and value["receipt"]["weight_target"]:
            f.window(value["receipt"]["weight_target"])

    assert runtime(f).sequence(stop=Event(), emit=receive)["status"] == "RESTORED"
    assert len(f.upstream.patches) == 4


def test_stopped_and_wrong_run_cannot_start(tmp_path):
    f = Fixture(tmp_path)
    stop = Event()
    stop.set()
    assert runtime(f).sequence(stop=stop)["status"] == "STOPPED"
    r = runtime(f)
    r.run_id = "another-run"
    with pytest.raises(EdgeError, match="registered_run_mismatch"):
        r.sequence(stop=Event())
    assert not f.upstream.patches


def test_ambiguous_start_stops_and_restart_only_reads(tmp_path):
    f = Fixture(tmp_path)
    f.upstream.patch_mode = "timeout_after"
    assert runtime(f).sequence(stop=Event())["status"] == "STOPPED"
    f.host = f.reopen()
    assert runtime(f).sequence(stop=Event())["status"] == "STOPPED"
    assert len(f.upstream.patches) == 1


def test_ambiguous_abort_is_not_reported_as_restored(tmp_path):
    f = Fixture(tmp_path)

    def receive(value):
        if "receipt" in value:
            f.window(10, fail=True)
            f.upstream.patch_mode = "timeout_after"

    result = runtime(f).sequence(stop=Event(), emit=receive)
    assert result["status"] == "STOPPED"
    assert result["rollback"]["status"] == "UNCERTAIN"
    assert len(f.upstream.patches) == 2


@pytest.mark.parametrize("interval", [0, -1, 6, float("nan"), True])
def test_invalid_interval_cannot_start_effects(tmp_path, interval):
    f = Fixture(tmp_path)
    with pytest.raises(ValueError):
        runtime(f).sequence(stop=Event(), interval_seconds=interval)
    assert not f.upstream.patches
