import base64

import pytest
from exec_schema import assert_valid

from rehearsal_runner.exec_contract import (
    MalformedRequest,
    args_sha256,
    inspect_record,
    probe_record,
)
from rehearsal_runner.exec_session import (
    ActionDenied,
    ActionFailed,
    ActionTimeout,
    ExecSession,
    Limits,
    NotSent,
    Outcome,
    Space,
    Transient,
)

PLANTED = "rehearsal-db-pass-7f3a9c"
SPACE = Space("rehearsal-plan-1-a", "uid-a")
PROBE_STATUS = {"kind": "probe", "probe_outcome": "CONNECTED", "http_status": 403, "elapsed_ms": 12}
INSPECT_STATUS = {"kind": "inspect", "found": True, "phase": "Running", "ready": True, "replicas": None,
                  "ready_replicas": None, "restarts": 0, "events": None}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Scripted:
    """An action whose run results are scripted: an Outcome or an exception per call."""

    def __init__(self, validate, *results, clock=None, cost=0.0):
        self._validate, self.results, self.calls, self.clock, self.cost = validate, list(results), [], clock, cost

    def validate(self, arguments):
        return self._validate(arguments)

    def run(self, arguments, deadline):
        self.calls.append((arguments, deadline))
        if self.clock is not None:
            self.clock.now += self.cost
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def apply_validate(arguments):
    if set(arguments) != {"edits"}:
        raise ValueError("not a proposal")
    return {"proposal_digest": "d" * 64}


def session(*, inspect=None, probe=None, apply=None, clock=None, uid="uid-a", limits=None, secrets=None):
    clock = clock or Clock()
    actions = {"inspect": inspect, "probe": probe, "apply": apply}
    return ExecSession("plan-1-a", 1, SPACE, actions, secrets=secrets or (lambda: [PLANTED]),
                       space_uid=lambda: uid, limits=limits, clock=clock, sleep=lambda s: None)


def request(tool, arguments, request_id="req-0001-abcd", **override):
    raw = {"schema_version": 1, "run_id": "plan-1-a", "attempt": 1, "request_id": request_id,
           "args_sha256": args_sha256(arguments), "tool": "rehearsal.exec." + tool, "arguments": arguments}
    return raw | override


LOGS = {"target_kind": "logs", "target_name": "chat-7d9f", "lines": 50}
POD = {"target_kind": "pod", "target_name": "chat-7d9f", "lines": None}
HTTP = {"service": "chat", "port": 8080, "method": "HTTP_GET", "path": "/health"}
DRAFT = {"edits": [{"path": "app/db_secret.py"}]}


def checked(s, response):
    assert_valid("response", response)
    for event in s.events():
        assert_valid("exec_event", event)
    assert_valid("exec_record", {"exec_events_status": "COMPLETE", "exec_events": s.events()})
    return response


def test_inspect_ok_records_checked_arguments_and_output_digest():
    action = Scripted(inspect_record, Outcome(INSPECT_STATUS, b"line one\nline two\n"))
    s = session(inspect=action)
    r = checked(s, s.handle(request("inspect", LOGS)))
    assert (r["result_code"], r["reason"], r["stage"]) == ("OK", None, None)
    assert r["output_tail"] == "line one\nline two\n" and r["budget"] == {"calls_left": 4, "seconds_left": 90}
    [event] = s.events()
    assert event["target_kind"] == "logs" and event["lines"] == 50 and event["output_bytes"] == 18
    assert "reason" not in event and "stage" not in event


def test_other_attempt_is_refused_and_not_recorded():
    s = session(inspect=Scripted(inspect_record, Outcome(INSPECT_STATUS)))
    r = checked(s, s.handle(request("inspect", POD, attempt=2)))
    assert (r["result_code"], r["reason"]) == ("REJECTED", "FOREIGN_ATTEMPT") and s.events() == []
    r = checked(s, s.handle(request("inspect", POD, run_id="plan-2-a")))
    assert r["reason"] == "FOREIGN_ATTEMPT" and s.events() == []


@pytest.mark.parametrize("override,stage", [
    ({"args_sha256": "0" * 64}, "ADMIT"),
    ({"schema_version": 2}, "ADMIT"),
    ({"arguments": POD | {"namespace": "kube-system"}, "args_sha256": args_sha256(POD | {"namespace": "x"})},
     "ADMIT"),
])
def test_envelope_problems_block_at_admit(override, stage):
    action = Scripted(inspect_record, Outcome(INSPECT_STATUS))
    s = session(inspect=action)
    r = checked(s, s.handle(request("inspect", POD) | override))
    assert (r["result_code"], r["reason"], r["stage"]) == ("REJECTED", "BLOCKED", stage)
    assert action.calls == []


@pytest.mark.parametrize("arguments", [
    POD | {"namespace": "kube-system"},
    {"target_kind": "pod", "target_name": None, "lines": None},
    {"target_kind": "logs", "target_name": "chat", "lines": None},
    {"target_kind": "logs", "target_name": "chat", "lines": 201},
    {"target_kind": "logs", "target_name": "chat", "lines": True},
])
def test_invalid_inspect_arguments_block_at_validate_without_argument_keys(arguments):
    action = Scripted(inspect_record, Outcome(INSPECT_STATUS))
    s = session(inspect=action)
    r = checked(s, s.handle(request("inspect", arguments)))
    assert (r["reason"], r["stage"]) == ("BLOCKED", "VALIDATE") and action.calls == []
    assert "target_kind" not in s.events()[0]


@pytest.mark.parametrize("path", [
    "/q?token=" + PLANTED,
    "/q?token=" + base64.b64encode(PLANTED.encode()).decode().rstrip("="),
    "/x" + base64.b64encode(b"ab" + PLANTED.encode()).decode().rstrip("="),
])
def test_rehearsal_secret_in_arguments_blocks_and_is_not_recorded(path):
    action = Scripted(probe_record, Outcome(PROBE_STATUS))
    s = session(probe=action)
    r = checked(s, s.handle(request("probe", HTTP | {"path": path})))
    assert (r["reason"], r["stage"]) == ("BLOCKED", "VALIDATE") and action.calls == []
    assert "path" not in s.events()[0]


def test_unreadable_secret_list_blocks():
    def broken():
        raise OSError("store unavailable")
    s = session(probe=Scripted(probe_record, Outcome(PROBE_STATUS)), secrets=broken)
    assert s.handle(request("probe", HTTP))["reason"] == "BLOCKED"


def test_changed_space_uid_blocks_before_execution():
    action = Scripted(inspect_record, Outcome(INSPECT_STATUS))
    for uid in ("uid-b", None):
        s = session(inspect=action, uid=uid)
        r = checked(s, s.handle(request("inspect", POD)))
        assert (r["reason"], r["stage"]) == ("BLOCKED", "ADMIT")
    assert action.calls == []


def test_disabled_action_is_blocked():
    s = session(apply=None)
    r = checked(s, s.handle(request("apply", DRAFT)))
    assert (r["result_code"], r["reason"]) == ("REJECTED", "BLOCKED")


def test_same_request_returns_stored_result_without_rerun():
    action = Scripted(inspect_record, Outcome(INSPECT_STATUS, b"x"))
    s = session(inspect=action)
    first = s.handle(request("inspect", POD))
    again = checked(s, s.handle(request("inspect", POD)))
    assert again == first and len(action.calls) == 1 and len(s.events()) == 1
    conflict = checked(s, s.handle(request("inspect", LOGS)))  # same ID, different arguments
    assert (conflict["reason"], conflict["stage"]) == ("BLOCKED", "ADMIT") and len(action.calls) == 1


@pytest.mark.parametrize("error", [Transient(), ActionTimeout(), RuntimeError("boom")])
def test_apply_with_unknown_effect_is_never_rerun_and_closes_exec(error):
    apply = Scripted(apply_validate, error)
    probe = Scripted(probe_record, Outcome(PROBE_STATUS))
    s = session(apply=apply, probe=probe)
    r = checked(s, s.handle(request("apply", DRAFT)))
    assert (r["result_code"], r["reason"], r["stage"]) == ("UNKNOWN", "EFFECT_UNKNOWN", "EXECUTE")
    assert len(apply.calls) == 1 and s.effect_unknown
    # The model calls again after the lost response: neither apply nor anything else runs.
    for tool, arguments, rid in (("apply", DRAFT, "req-0002-abcd"), ("probe", HTTP, "req-0003-abcd")):
        later = checked(s, s.handle(request(tool, arguments, request_id=rid)))
        assert (later["result_code"], later["reason"], later["stage"]) == ("REJECTED", "EFFECT_UNKNOWN", "ADMIT")
    assert len(apply.calls) == 1 and probe.calls == []
    assert [e["result_code"] for e in s.events()] == ["UNKNOWN", "REJECTED", "REJECTED"]
    assert s.events()[0]["proposal_digest"] == "d" * 64


def test_not_sent_is_retried_within_limits_then_unavailable():
    action = Scripted(probe_record, NotSent())
    s = session(probe=action)
    r = checked(s, s.handle(request("probe", HTTP)))
    assert (r["result_code"], r["reason"], r["stage"]) == ("FAILED", "UNAVAILABLE", "EXECUTE")
    assert len(action.calls) == 3  # first try plus two retries
    assert r["budget"]["calls_left"] == 4  # retries are not calls


def test_apply_not_sent_is_retried_and_succeeds():
    ok = Outcome({"kind": "apply", "proposal_digest": "d" * 64, "targets": [{"kind": "deployment", "name": "chat"}]},
                 record={"proposal_digest": "d" * 64})
    action = Scripted(apply_validate, NotSent(), ok)
    s = session(apply=action)
    r = checked(s, s.handle(request("apply", DRAFT)))
    assert r["result_code"] == "OK" and len(action.calls) == 2 and not s.effect_unknown


def test_read_transient_is_retried():
    action = Scripted(inspect_record, Transient(), Outcome(INSPECT_STATUS))
    s = session(inspect=action)
    assert checked(s, s.handle(request("inspect", POD)))["result_code"] == "OK" and len(action.calls) == 2


def test_no_retry_when_remaining_time_is_too_short():
    clock = Clock()
    action = Scripted(probe_record, NotSent(), clock=clock, cost=14.5)
    s = session(probe=action, clock=clock)
    s.handle(request("probe", HTTP))
    assert len(action.calls) == 1


@pytest.mark.parametrize("error,code,reason", [
    (ActionDenied(), "REJECTED", "BLOCKED"),
    (ActionTimeout(), "FAILED", "TIMEOUT"),
    (ActionFailed(b"admission webhook denied the request\n"), "FAILED", "NONZERO_EXIT"),
])
def test_read_failures_map_to_fixed_reasons(error, code, reason):
    s = session(probe=Scripted(probe_record, error))
    r = checked(s, s.handle(request("probe", HTTP)))
    assert (r["result_code"], r["reason"], r["stage"]) == (code, reason, "EXECUTE")
    assert s.events()[0]["reason"] == reason
    if reason == "NONZERO_EXIT":
        assert r["output_tail"] == "admission webhook denied the request\n"


def test_five_slow_calls_fit_and_the_sixth_is_refused():
    clock = Clock()
    action = Scripted(probe_record, Outcome(PROBE_STATUS), clock=clock, cost=15)
    s = session(probe=action, clock=clock)
    for n in range(5):
        r = checked(s, s.handle(request("probe", HTTP, request_id=f"req-000{n}-slow")))
        assert r["result_code"] == "OK"
        deadline = action.calls[-1][1]
        assert deadline - (clock.now - 15) <= 15
    assert clock.now - 1000 == 75  # within the 90 s exec budget, leaving time to submit
    r = checked(s, s.handle(request("probe", HTTP, request_id="req-0006-slow")))
    assert (r["result_code"], r["reason"]) == ("REJECTED", "BUDGET_EXHAUSTED")
    assert r["budget"]["calls_left"] == 0 and len(action.calls) == 5


def test_total_exec_time_budget():
    clock = Clock()
    action = Scripted(probe_record, Outcome(PROBE_STATUS))
    s = session(probe=action, clock=clock)
    s.handle(request("probe", HTTP, request_id="req-0001-time"))
    clock.now += 80
    s.handle(request("probe", HTTP, request_id="req-0002-time"))
    assert action.calls[-1][1] - clock.now == pytest.approx(10)  # only the rest of the 90 s
    clock.now += 10
    r = s.handle(request("probe", HTTP, request_id="req-0003-time"))
    assert r["reason"] == "BUDGET_EXHAUSTED" and len(action.calls) == 2


def test_secret_in_output_is_withheld_even_across_the_tail_cut():
    output = PLANTED.encode()[:10] + b"x" * 10 + b"\n" + base64.b64encode(PLANTED.encode()) + b"y" * 4000
    s = session(inspect=Scripted(inspect_record, Outcome(INSPECT_STATUS, output)))
    r = checked(s, s.handle(request("inspect", LOGS)))
    assert (r["result_code"], r["reason"], r["stage"]) == ("OK", "OUTPUT_WITHHELD", "OUTPUT")
    assert r["output_tail"] is None and r["output_truncated"] is False
    event = s.events()[0]
    assert event["reason"] == "OUTPUT_WITHHELD" and event["output_bytes"] == len(output)


@pytest.mark.parametrize("output", [b"\xff\xfe binary", "ok".encode("utf-16")])
def test_non_utf8_output_is_withheld(output):
    s = session(inspect=Scripted(inspect_record, Outcome(INSPECT_STATUS, output)))
    assert s.handle(request("inspect", LOGS))["reason"] == "OUTPUT_WITHHELD"


def test_tail_is_last_2kib_on_a_character_boundary():
    output = ("가" * 1000).encode()  # 3000 bytes of 3-byte characters
    s = session(inspect=Scripted(inspect_record, Outcome(INSPECT_STATUS, output)))
    r = checked(s, s.handle(request("inspect", LOGS)))
    assert r["output_truncated"] is True and len(r["output_tail"].encode()) <= 2048
    assert r["output_tail"] == "가" * (2048 // 3)


def test_session_expiry_and_close_refuse_without_records():
    clock = Clock()
    action = Scripted(probe_record, Outcome(PROBE_STATUS))
    s = session(probe=action, clock=clock)
    clock.now += 180
    r = checked(s, s.handle(request("probe", HTTP)))
    assert (r["reason"], r["stage"]) == ("UNAVAILABLE", "ADMIT") and s.events() == []
    s2 = session(probe=action)
    s2.close()
    assert s2.handle(request("probe", HTTP))["reason"] == "UNAVAILABLE" and action.calls == []


def test_record_limit_closes_the_session():
    s = session(inspect=Scripted(inspect_record, Outcome(INSPECT_STATUS)), limits=Limits(records=3, calls=10))
    for n in range(3):
        s.handle(request("inspect", POD, request_id=f"req-000{n}-full"))
    r = s.handle(request("inspect", POD, request_id="req-0009-full"))
    assert (r["reason"], r["stage"]) == ("UNAVAILABLE", "ADMIT") and len(s.events()) == 3


def test_unreadable_request_is_a_channel_error():
    s = session()
    for raw in (None, {"tool": "rehearsal.exec.inspect"}, request("inspect", POD) | {"tool": "rehearsal.exec"},
                request("inspect", POD, request_id="short")):
        with pytest.raises(MalformedRequest):
            s.handle(raw)
