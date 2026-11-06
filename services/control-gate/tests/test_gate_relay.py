"""T19 operator relay (platform side) and issuer CLI (operator side) together on a real T10 store.

The keys are created by the CLI in a private directory; the relay only ever receives public keys,
T13 records and signed envelopes, and acts as the subjects fixed in its config.
"""

import importlib.util
import os
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
    for purpose, key in keys.items():
        assert f.call(cli.enroll_request(f.plan.plan_id, purpose, key, f.clock.at, 6))["enrolled"]["revision"] == 1
    f.clock.at = f.state["now"]
    for side in ("source", "target"):
        f.records.record_run(f.operator, f.plan.plan_id, side, f.state["runs"][side])
    f.records.bind(f.operator, f.plan.plan_id, "context", f.state["context"], expected_revision=0)
    f.call({"op": "readiness", "plan_id": f.plan.plan_id, "binding": asdict(f.data["binding"]),
            "snapshot": {"ref": f.data["snapshot"].ref, "document": f.data["snapshot"].document}})
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    assert "gate-result" not in subjects  # nothing evaluated yet
    context = subjects["context"]["digest"]
    purposes = ["context", "run-source", "run-target", "readiness-binding", "readiness-snapshot"]
    for request in cli.sign_requests(f.plan.plan_id, subjects, keys, purposes, context, f.clock.at,
                                     timedelta(minutes=5)):
        f.call(request)
    assert f.evaluate()["decision"] == "PASS"
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    [request] = cli.sign_requests(f.plan.plan_id, subjects, keys, ["gate-result"], context, f.clock.at,
                                  timedelta(minutes=5))
    assert f.call(request)["published"]["purpose"] == "gate-result"


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
    with pytest.raises(ValueError, match="context_not_confirmed"):
        cli.sign_requests(f.plan.plan_id, subjects, keys, ["run-source"], "c" * 64, f.clock.at, timedelta(minutes=5))
    with pytest.raises(ValueError, match="subject_missing:gate-result"):
        cli.sign_requests(f.plan.plan_id, subjects, keys, ["gate-result"], "a" * 64, f.clock.at, timedelta(minutes=5))
    with pytest.raises(ValueError, match="envelope_ttl_out_of_range"):
        cli.sign_requests(f.plan.plan_id, subjects, keys, ["run-source"], "a" * 64, f.clock.at, timedelta(hours=25))


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
    for purpose, key in keys.items():
        f.call(cli.enroll_request(f.plan.plan_id, purpose, key, f.clock.at, 6))
    for side in ("source", "target"):
        f.records.record_run(f.operator, f.plan.plan_id, side, f.state["runs"][side])
    f.records.bind(f.operator, f.plan.plan_id, "context", f.state["context"], expected_revision=0)
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    [request] = cli.sign_requests(f.plan.plan_id, subjects, keys, ["run-source"], subjects["context"]["digest"],
                                  f.clock.at, timedelta(minutes=5))
    with pytest.raises(GateError):
        f.call(request)
