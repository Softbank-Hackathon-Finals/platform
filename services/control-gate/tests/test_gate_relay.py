"""T19 operator relay (platform side) and issuer CLI (operator side) together on a real T10 store.

The keys are created by the CLI in a private directory; the relay only ever receives public keys,
T13 records and signed envelopes, and acts as the subjects fixed in its config.
"""

import importlib.util
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest
from bravo_platform_events import gate_relay
from test_gate_assembly import gate  # noqa: F401
from test_readiness import ready  # noqa: F401

from bravo_gate.contract import GateError

spec = importlib.util.spec_from_file_location(
    "gate_cli", Path(__file__).resolve().parents[3] / "operations/gate/run.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture
def relay(gate, tmp_path):  # noqa: F811 - imported shared fixture
    config = {"tenant_id": gate.state["context"].tenant_id, "admin": "gate-admin",
              "issuer": "gate-issuer", "operator": "gate-operator"}
    gate.config = config
    gate.call = lambda request: gate_relay.handle(gate.records, request, config, clock=lambda: gate.clock.at)
    gate.keys_dir = tmp_path / "keys"
    return gate


def test_issuer_enrolls_records_signs_and_the_gate_passes(relay):
    f = relay
    keys = cli.load_keys(f.keys_dir, create=True)
    assert set(keys) == set(cli.PURPOSES) and oct(os.stat(f.keys_dir).st_mode & 0o777) == "0o700"
    for request in cli.enroll_requests(f.keys_dir, f.plan.plan_id, keys, cli.PURPOSES, f.clock.at, 6):
        assert f.call(request)["enrolled"]["revision"] == 1
    f.clock.at = f.state["now"]
    for side in ("source", "target"):
        f.records.record_run(f.operator, f.plan.plan_id, side, f.state["runs"][side])
    f.records.bind(f.operator, f.plan.plan_id, "context", f.state["context"], expected_revision=0)
    f.call({"op": "readiness", "plan_id": f.plan.plan_id, "binding": asdict(f.data["binding"]),
            "snapshot": {"ref": f.data["snapshot"].ref, "document": f.data["snapshot"].document}})
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    assert "gate-result" not in subjects and subjects["published"] == []  # nothing evaluated or signed yet
    # The documented order: `sign` with defaults signs what is shown and not yet published.
    for request in cli.sign_requests(f.plan.plan_id, subjects, keys, None, subjects["confirm"], f.clock.at,
                                     timedelta(minutes=5)):
        f.call(request)
    assert f.evaluate()["decision"] == "PASS"
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    assert set(subjects["published"]) == set(cli.PURPOSES) - {"gate-result"}
    [request] = cli.sign_requests(f.plan.plan_id, subjects, keys, None, subjects["confirm"], f.clock.at,
                                  timedelta(minutes=5))
    assert f.call(request)["published"]["purpose"] == "gate-result"
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    assert cli.sign_requests(f.plan.plan_id, subjects, keys, None, subjects["confirm"], f.clock.at,
                             timedelta(minutes=5)) == []  # run again: nothing left, no conflict


def test_a_readiness_record_added_after_the_review_is_never_signed(relay):
    f = relay
    keys = cli.load_keys(f.keys_dir, create=True)
    for request in cli.enroll_requests(f.keys_dir, f.plan.plan_id, keys, cli.PURPOSES, f.clock.at, 6):
        f.call(request)
    f.clock.at = f.state["now"]
    for side in ("source", "target"):
        f.records.record_run(f.operator, f.plan.plan_id, side, f.state["runs"][side])
    f.records.bind(f.operator, f.plan.plan_id, "context", f.state["context"], expected_revision=0)
    reviewed = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    f.call({"op": "readiness", "plan_id": f.plan.plan_id, "binding": asdict(f.data["binding"]),
            "snapshot": {"ref": f.data["snapshot"].ref, "document": f.data["snapshot"].document}})
    now = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    assert now["context"] == reviewed["context"]  # the same context: only the T13 record is new
    with pytest.raises(ValueError, match="subjects_not_confirmed"):
        cli.sign_requests(f.plan.plan_id, now, keys, None, reviewed["confirm"], f.clock.at, timedelta(minutes=5))


def test_a_partly_done_enrollment_can_be_run_again(relay):
    f = relay
    keys = cli.load_keys(f.keys_dir, create=True)
    for request in cli.enroll_requests(f.keys_dir, f.plan.plan_id, keys, ["context", "run-source"], f.clock.at, 6):
        f.call(request)
    f.clock.at += timedelta(minutes=3)  # a later retry with all purposes
    results = [f.call(r)["enrolled"] for r in cli.enroll_requests(f.keys_dir, f.plan.plan_id, keys, cli.PURPOSES,
                                                                   f.clock.at, 6)]
    assert [r["revision"] for r in results] == [1] * len(cli.PURPOSES)
    with pytest.raises(ValueError, match="key_validity_out_of_range"):
        cli.enroll_requests(f.keys_dir, "another-plan", keys, ["context"], f.clock.at, 49)


def test_the_request_cannot_choose_who_acts(relay):
    f = relay
    with pytest.raises(ValueError, match="gate_relay_request_invalid"):
        f.call({"op": "subjects", "plan_id": f.plan.plan_id, "principal": "gate-admin"})
    with pytest.raises(ValueError, match="gate_relay_request_invalid"):
        f.call({"op": "approve", "plan_id": f.plan.plan_id})
    with pytest.raises(ValueError, match="gate_relay_configuration_invalid"):
        gate_relay.handle(f.records, {"op": "subjects", "plan_id": f.plan.plan_id},
                          dict(f.config, issuer="gate-admin"))


def test_an_unreviewed_context_or_a_long_lived_envelope_is_never_signed(relay):
    f = relay
    keys = cli.load_keys(f.keys_dir, create=True)
    subjects = {"context": {"gate_id": "g", "digest": "a" * 64}, "run-source": "b" * 64}
    confirm = cli.fingerprint(subjects)
    subjects |= {"confirm": confirm, "published": []}
    with pytest.raises(ValueError, match="subjects_not_confirmed"):
        cli.sign_requests(f.plan.plan_id, subjects, keys, ["run-source"], "c" * 64, f.clock.at, timedelta(minutes=5))
    with pytest.raises(ValueError, match="subject_missing:gate-result"):
        cli.sign_requests(f.plan.plan_id, subjects, keys, ["gate-result"], confirm, f.clock.at, timedelta(minutes=5))
    with pytest.raises(ValueError, match="envelope_ttl_out_of_range"):
        cli.sign_requests(f.plan.plan_id, subjects, keys, ["run-source"], confirm, f.clock.at, timedelta(hours=25))


def test_keys_must_stay_private(relay, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    with pytest.raises(ValueError, match="key_directory_must_be_private"):
        cli.load_keys(shared, create=True)


def test_enrollment_by_the_relay_cannot_vouch_for_an_earlier_collection(relay):
    f = relay
    keys = cli.load_keys(f.keys_dir, create=True)
    f.clock.at = f.state["now"]  # enrolled after the T14 runs started
    for request in cli.enroll_requests(f.keys_dir, f.plan.plan_id, keys, cli.PURPOSES, f.clock.at, 6):
        f.call(request)
    for side in ("source", "target"):
        f.records.record_run(f.operator, f.plan.plan_id, side, f.state["runs"][side])
    f.records.bind(f.operator, f.plan.plan_id, "context", f.state["context"], expected_revision=0)
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    [request] = cli.sign_requests(f.plan.plan_id, subjects, keys, ["run-source"], subjects["confirm"],
                                  f.clock.at, timedelta(minutes=5))
    with pytest.raises(GateError):
        f.call(request)


def test_a_relay_failure_is_reported_as_failed_not_as_a_hold(monkeypatch):
    def relay(code):
        return cli.relay_with([sys.executable, "-c", code])
    with pytest.raises(RuntimeError, match="hold:gate_readiness_unavailable"):
        relay("print('{\"status\": \"HOLD\", \"code\": \"gate_readiness_unavailable\"}')")({})
    with pytest.raises(cli.RelayFailed, match="ImportError"):
        relay("print('{\"status\": \"FAIL\", \"reason\": \"ImportError\"}')")({})
    with pytest.raises(cli.RelayFailed, match="pods is forbidden"):
        relay("import sys; sys.stderr.write('Error from server: pods is forbidden')")({})

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 120)
    monkeypatch.setattr(cli.subprocess, "run", timeout)
    with pytest.raises(cli.RelayFailed, match="relay_timeout"):
        relay("pass")({})
    assert cli.main(["subjects", "--plan-id", "p", "--relay", "x"]) == 1  # FAILED, exit 1
    with pytest.raises(SystemExit):
        cli.main(["subjects"])  # no plan or cluster: usage error
