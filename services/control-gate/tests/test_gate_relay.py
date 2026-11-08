"""T19 operator relay (platform side) and issuer CLI (operator side) together on a real T10 store.

The keys are created by the CLI in a private directory; the relay only ever receives public keys,
T13 records and signed envelopes, and acts as the subjects fixed in its config.
"""

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from bravo_platform_events import gate_relay
from bravo_platform_events.store import EventError
from test_gate_assembly import gate  # noqa: F401
from test_readiness import ready  # noqa: F401
from test_t13_executor_observation import FakeDb, site

from bravo_gate.contract import GateError, fingerprint, time

spec = importlib.util.spec_from_file_location(
    "gate_cli", Path(__file__).resolve().parents[3] / "operations/gate/run.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


@pytest.fixture
def relay(gate, tmp_path):  # noqa: F811 - imported shared fixture
    config = {"tenant_id": gate.state["context"].tenant_id, "admin": "gate-admin",
              "issuer": "gate-issuer", "operator": "gate-operator"}
    gate.config = config
    gate.call = lambda request: gate_relay.handle(gate.records, request, config, clock=lambda: gate.clock.at,
                                                  access=gate.access)
    gate.keys_dir = tmp_path / "keys"
    return gate


def run_doc(f, side, **changes):
    doc = json.loads(f.state["runs"][side].raw)
    doc.update(changes.pop("fields", {}))
    for row in doc["results"]:
        row.update(changes.get(row["test_id"], {}))
    return doc


def shifted(doc, delta):
    for key in ("started_at", "finished_at"):
        doc[key] = (time(doc[key]) + delta).isoformat()
    for row in doc["results"]:
        if row["evidence"] is not None:
            for key in ("collected_at", "expires_at"):
                row["evidence"][key] = (time(row["evidence"][key]) + delta).isoformat()
    return doc


def bind_through_the_relay(f, source, target):
    """record + bind as an operator does at S1; the T13 capture is then made for the bound context."""
    for side, doc in (("source", source), ("target", target)):
        f.call({"op": "record", "plan_id": f.plan.plan_id, "side": side, "document": doc})
    bound = f.call({"op": "bind", "plan_id": f.plan.plan_id})["bound"]
    f.state["context"] = replace(f.base, gate_id=bound["gate_id"], source_ref=source["run_id"],
                                 target_ref=target["run_id"], source_digest=fingerprint(source),
                                 target_digest=fingerprint(target))
    assert f.state["context"].fingerprint == bound["context"]
    f.update(f.doc)
    f.call({"op": "readiness", "plan_id": f.plan.plan_id, "binding": asdict(f.data["binding"]),
            "snapshot": {"ref": f.data["snapshot"].ref, "document": f.data["snapshot"].document}})
    return bound


def test_issuer_enrolls_records_signs_and_the_gate_passes(relay):
    f = relay
    keys = cli.load_keys(f.keys_dir, create=True)
    assert set(keys) == set(cli.PURPOSES) and oct(os.stat(f.keys_dir).st_mode & 0o777) == "0o700"
    for request in cli.enroll_requests(f.keys_dir, f.plan.plan_id, keys, cli.PURPOSES, f.clock.at, 6):
        assert f.call(request)["enrolled"]["revision"] == 1
    f.clock.at = f.state["now"]
    f.base = f.state["context"]
    bind_through_the_relay(f, run_doc(f, "source"), run_doc(f, "target"))
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    assert "gate-result" not in subjects and subjects["published"] == []  # nothing evaluated or signed yet
    # The documented order: `sign` with defaults signs what is shown and not yet published.
    for request in cli.sign_requests(f.plan.plan_id, subjects, keys, None, subjects["confirm"], f.clock.at,
                                     timedelta(minutes=5)):
        f.call(request)
    assert f.call({"op": "evaluate", "plan_id": f.plan.plan_id})["evaluated"]["decision"] == "PASS"
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    assert set(subjects["published"]) == set(cli.PURPOSES) - {"gate-result"}
    [request] = cli.sign_requests(f.plan.plan_id, subjects, keys, None, subjects["confirm"], f.clock.at,
                                  timedelta(minutes=5))
    assert f.call(request)["published"]["purpose"] == "gate-result"
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    assert cli.sign_requests(f.plan.plan_id, subjects, keys, None, subjects["confirm"], f.clock.at,
                             timedelta(minutes=5)) == []  # run again: nothing left, no conflict


def test_s1_records_binds_and_evaluates_the_first_gate_through_the_relay(relay):
    f = relay
    keys = cli.load_keys(f.keys_dir, create=True)
    for request in cli.enroll_requests(f.keys_dir, f.plan.plan_id, keys, cli.PURPOSES, f.clock.at, 6):
        f.call(request)
    f.clock.at = f.state["now"]
    f.base = f.state["context"]
    source = run_doc(f, "source")
    f.call({"op": "record", "plan_id": f.plan.plan_id, "side": "source", "document": source})
    with pytest.raises(EventError, match="gate_run_required"):
        f.call({"op": "bind", "plan_id": f.plan.plan_id})  # one side only
    open_bucket = run_doc(f, "target", fields={"run_id": "run-open-bucket"},
                          **{"IAM-01.1": {"verdict": "FAIL", "observed": "allow"}})
    bound = bind_through_the_relay(f, source, open_bucket)
    assert f.call({"op": "bind", "plan_id": f.plan.plan_id})["bound"] == bound  # the same runs again
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    for request in cli.sign_requests(f.plan.plan_id, subjects, keys, None, subjects["confirm"], f.clock.at,
                                     timedelta(minutes=5)):
        f.call(request)
    assert f.call({"op": "evaluate", "plan_id": f.plan.plan_id})["evaluated"] == {
        "decision": "BLOCK", "security": "BLOCK", "data": "PASS", "context": bound["context"]}
    with pytest.raises(ValueError, match="gate_relay_access_required"):
        gate_relay.handle(f.records, {"op": "evaluate", "plan_id": f.plan.plan_id}, f.config,
                          clock=lambda: f.clock.at)
    # A newer target run (taken after the fix) is what the next bind uses: a new context, never the old one.
    fixed = shifted(run_doc(f, "target", fields={"run_id": "run-fixed"}), timedelta(seconds=20))
    f.call({"op": "record", "plan_id": f.plan.plan_id, "side": "target", "document": fixed})
    again = f.call({"op": "bind", "plan_id": f.plan.plan_id})["bound"]
    assert again["gate_id"] != bound["gate_id"] and again["revision"] == bound["revision"] + 1


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


def executor_observation(f):
    from bravo_cutover.db_fingerprint import SequenceState
    from bravo_cutover.readiness import Readiness, ReadinessRequest
    from bravo_cutover.remote.db import Subscription
    from bravo_cutover.sites import Config, Settings
    from bravo_platform_events.readiness_observer import request_for

    ctx = f.state["context"]
    world = {"schema": {"src": {}, "dst": {}}, "sub": Subscription(enabled=True, workers=1), "login": False,
             "seq": {"src": SequenceState(20, True, 20, 2**63 - 1), "dst": SequenceState(3, True, 20, 2**63 - 1)}}
    config = Config(settings=Settings(), origin=ctx.source_environment_id,
                    sites={ctx.source_environment_id: site(ctx.source_environment_id, "src"),
                           ctx.target_environment_id: site(ctx.target_environment_id, "dst")})
    pair = {side: {"environment_id": getattr(ctx, side + "_environment_id")} for side in ("source", "target")}
    executor = Readiness(config, db=lambda dsn, control_functions: FakeDb(world, dsn), clock=lambda: f.clock.at)
    return executor.observe(ReadinessRequest(**request_for(pair)))


def capture_args(f, evaluate=True):
    return SimpleNamespace(command="capture", plan_id=f.plan.plan_id, dir=f.keys_dir, ttl_minutes=5,
                           evaluate=evaluate)


def signed_context_and_runs(f):
    """S1 as a person does it: record, bind, review `subjects` and sign the context and both runs."""
    keys = cli.load_keys(f.keys_dir, create=True)
    for request in cli.enroll_requests(f.keys_dir, f.plan.plan_id, keys, cli.PURPOSES, f.clock.at, 6):
        f.call(request)
    f.clock.at = f.state["now"]
    f.base = f.state["context"]
    bind_through_the_relay(f, run_doc(f, "source"), run_doc(f, "target"))
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    for request in cli.sign_requests(f.plan.plan_id, subjects, keys, ["context", "run-source", "run-target"],
                                     subjects["confirm"], f.clock.at, timedelta(minutes=5)):
        f.call(request)


def test_capture_records_the_executor_observation_signs_it_and_the_gate_passes(relay):
    f = relay
    signed_context_and_runs(f)
    observation = executor_observation(f)
    out = cli.run(capture_args(f), relay=f.call, now=f.clock.at, observe=lambda _: {"observation": observation},
                  clock=lambda: f.clock.at)
    assert [p["purpose"] for p in out["published"]] == list(cli.READINESS)
    assert out["evaluated"]["decision"] == "PASS" and out["evaluated"]["data"] == "PASS"
    assert out["readiness"]["diagnostics"]["sequences"] == {}
    assert out["readiness"]["data"]["decision"] == "PASS" and out["readiness"]["data"]["blocked"] == []
    # A later pass for the same context (before SHIFT or cutover): a new record, signed, not evaluated again
    f.clock.at += timedelta(seconds=31)
    later = executor_observation(f)
    again = cli.run(capture_args(f, evaluate=False), relay=f.call, now=f.clock.at,
                    observe=lambda _: {"observation": later}, clock=lambda: f.clock.at)
    assert again["readiness"]["binding"] != out["readiness"]["binding"] and "evaluated" not in again
    assert again["readiness"]["data"]["decision"] == "PASS" and len(again["published"]) == 2
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    assert subjects["readiness-binding"] == again["readiness"]["binding"]


def test_a_data_block_is_signed_but_never_spends_the_context_unless_asked(relay):
    f = relay
    signed_context_and_runs(f)
    blocked = executor_observation(f)
    blocked["replication"]["lag_bytes"] = None  # replication unknown at this moment
    evaluated = []

    def watching(request):
        if request["op"] == "evaluate":
            evaluated.append(request)
        return f.call(request)

    with pytest.raises(RuntimeError, match="hold:readiness_data_block") as held:
        cli.run(capture_args(f), relay=watching, now=f.clock.at, observe=lambda _: {"observation": blocked},
                clock=lambda: f.clock.at)
    assert evaluated == []
    # The hold says why (fixed row codes) and which readiness signatures are already published (#612 follow-up)
    assert held.value.detail["blocked"] == [{"test_id": "replication:lag", "reason": "UNKNOWN"}]
    subjects = f.call({"op": "subjects", "plan_id": f.plan.plan_id})["subjects"]
    assert held.value.detail["published"] == [{"purpose": p, "subject": subjects[p]} for p in cli.READINESS]
    args = capture_args(f)
    args.record_block = True  # S1 may record its first BLOCK on purpose
    f.clock.at += timedelta(seconds=1)
    again = executor_observation(f)
    again["replication"]["lag_bytes"] = None
    out = cli.run(args, relay=watching, now=f.clock.at, observe=lambda _: {"observation": again},
                  clock=lambda: f.clock.at)
    assert out["evaluated"]["data"] == "BLOCK" and len(evaluated) == 1


def test_the_operator_reads_a_hold_detail_on_one_line(monkeypatch, capsys):
    detail = {"blocked": [{"test_id": "replication:lag", "reason": "UNKNOWN"}],
              "published": [{"purpose": "readiness-binding", "subject": "a" * 64}]}

    def hold(*_a, **_k):
        raise cli.Hold("readiness_data_block", **detail)
    monkeypatch.setattr(cli, "run", hold)
    assert cli.main(["subjects", "--plan-id", "p", "--relay", "x"]) == 10
    assert json.loads(capsys.readouterr().out) == {"status": "HOLD", "reason": "hold:readiness_data_block", **detail}


def test_a_pass_that_would_expire_before_the_evaluation_holds_instead_of_spending_the_context(relay):
    """Live S1 (11/8): signing took the 30 s record past its validity and the GATE recorded a freshness BLOCK."""
    f = relay
    signed_context_and_runs(f)
    observation = executor_observation(f)
    evaluated = []

    def slow(request):
        if request["op"] == "publish":
            f.clock.at += timedelta(seconds=12)  # one kubectl exec round trip each
        if request["op"] == "evaluate":
            evaluated.append(request)
        return f.call(request)

    with pytest.raises(RuntimeError, match="hold:readiness_expires_before_evaluate"):
        cli.run(capture_args(f), relay=slow, now=f.clock.at, observe=lambda _: {"observation": observation},
                clock=lambda: f.clock.at)
    assert evaluated == []  # the context is still unevaluated: the next capture can record its decision


def test_one_relay_session_answers_many_requests_in_order():
    import sys

    fake = ("import json, sys\nprint(json.dumps({'status': 'READY'}), flush=True)\n"
            "for line in sys.stdin:\n"
            "    r = json.loads(line)\n"
            "    print(json.dumps({'status': 'HOLD', 'code': 'NO'} if r['op'] == 'evaluate' else"
            " {'status': 'PASS', 'op': r['op']}), flush=True)\n")
    with cli.Session([sys.executable, "-c", fake]) as call:
        assert [call({"op": op})["op"] for op in ("capture", "subjects", "publish")] == ["capture", "subjects", "publish"]
        with pytest.raises(RuntimeError, match="hold:NO"):
            call({"op": "evaluate"})
    with pytest.raises(cli.RelayFailed, match="relay_session_unavailable"):
        cli.Session([sys.executable, "-c", "print('no')"]).__enter__()


def test_capture_signs_nothing_when_another_record_lands_first(relay):
    f = relay
    signed_context_and_runs(f)
    observation = executor_observation(f)
    published = []

    def racing(request):
        answer = f.call(request)
        if request["op"] == "capture":  # someone else's T13 record lands before `subjects`
            f.update(f.doc | {"sequences": []})
            f.call({"op": "readiness", "plan_id": f.plan.plan_id, "binding": asdict(f.data["binding"]),
                    "snapshot": {"ref": f.data["snapshot"].ref, "document": f.data["snapshot"].document}})
        if request["op"] == "publish":
            published.append(request)
        return answer

    with pytest.raises(RuntimeError, match="hold:readiness_record_superseded"):
        cli.run(capture_args(f), relay=racing, now=f.clock.at, observe=lambda _: {"observation": observation},
                clock=lambda: f.clock.at)
    assert published == []


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
