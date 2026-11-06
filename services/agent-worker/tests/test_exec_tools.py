"""T42 rehearsal.exec v1, T11 side. The contract schema file is read here, not copied."""

import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage
from jsonschema import Draft202012Validator
from test_openai import install_sdk

from bravo_agent import exec_tools
from bravo_agent.contracts import Proposal, digest
from bravo_agent.coordinator import Coordinator
from bravo_agent.exec_tools import (
    EXEC_FUNCTIONS,
    EXEC_TOOLS,
    INSPECT_PARAMETERS,
    PROBE_PARAMETERS,
    ExecReserve,
    check_response,
    exec_envelope,
    inspect_arguments,
    local_response,
    probe_arguments,
)
from bravo_agent.openai_provider import (
    RESERVED_FUNCTIONS,
    OpenAIProvider,
    function_tools,
    strict_schema,
)
from bravo_agent.tools import RESERVED_TOOLS, PolicyDenied, Tools

CONTRACT = Path(__file__).resolve().parents[3] / "docs/contracts/T42-rehearsal-exec-v1.schema.json"
SCHEMA = json.loads(CONTRACT.read_text())
DEFS = SCHEMA["$defs"]


def validator(*names):
    return Draft202012Validator({"$defs": DEFS, "allOf": [{"$ref": f"#/$defs/{name}"} for name in names]})


INSPECT = validator("inspect_args", "inspect_rules")
PROBE = validator("probe_args", "probe_rules")
LOCAL_REQUEST = validator("local_request")
RESPONSE = validator("response")


def ok_response(envelope, **changes):
    action = envelope["tool"].rsplit(".", 1)[1]
    status = {
        "apply": {"kind": "apply", "proposal_digest": envelope["args_sha256"],
                  "targets": [{"kind": "deployment", "name": "candidate"}]},
        "inspect": {"kind": "inspect", "found": True, "phase": "Running", "ready": True, "replicas": None,
                    "ready_replicas": None, "restarts": 0, "events": None},
        "probe": {"kind": "probe", "probe_outcome": "CONNECTED", "http_status": 403, "elapsed_ms": 12},
    }[action]
    return {"schema_version": 1, "request_id": envelope["request_id"], "action": action, "result_code": "OK",
            "reason": None, "stage": None, "status": status, "output_tail": "candidate ready",
            "output_truncated": False, "budget": {"calls_left": 4, "seconds_left": 80}} | changes


class FakeChannel:
    """Test double for the T38 broker -> T15 exec channel."""

    def __init__(self, reply=ok_response, enabled=True):
        self.enabled, self.reply, self.sent = enabled, reply, []

    def send(self, envelope):
        self.sent.append(copy.deepcopy(envelope))
        return self.reply(envelope)


def open_tools(context, channel, **kwargs):
    bundle, snapshot, _, _ = context
    tools = Tools(bundle, snapshot, 1, exec_channel=channel, exec_enabled=True, **kwargs)
    tools.set_model_budget(7, time.monotonic() + 180)
    return tools


INSPECT_OK = {"target_kind": "logs", "target_name": "private-pod", "lines": 50}
PROBE_OK = {"service": "private-svc", "port": 8080, "method": "HTTP_GET", "path": "/private-path"}


# --- contract file -------------------------------------------------------------------------------

def test_contract_file_is_the_pinned_v1_and_the_model_schemas_match_it():
    assert hashlib.sha256(CONTRACT.read_bytes()).hexdigest() == exec_tools.CONTRACT_SHA256
    for name, ours in (("inspect_args", INSPECT_PARAMETERS), ("probe_args", PROBE_PARAMETERS)):
        assert {k: v for k, v in DEFS[name].items() if k != "description"} == ours
    assert sorted(EXEC_TOOLS.values()) == sorted(DEFS["tool"]["enum"])
    assert sorted(exec_tools.EXEC_ACTIONS.values()) == sorted(DEFS["action"]["enum"])
    assert list(exec_tools.RESULT_CODES) == DEFS["result_code"]["enum"]
    assert list(exec_tools.REASONS) == DEFS["reason"]["enum"]
    assert list(exec_tools.STAGES) == DEFS["stage"]["enum"]
    assert list(exec_tools.PROBE_OUTCOMES) == DEFS["probe_outcome"]["enum"]
    assert (exec_tools.REQUEST_ID, exec_tools.SHA256, exec_tools.K8S_NAME, exec_tools.HTTP_PATH) == (
        DEFS["request_id"]["pattern"], DEFS["sha256"]["pattern"], DEFS["k8s_name"]["pattern"],
        DEFS["http_path"]["pattern"])
    assert exec_tools.RESPONSE_KEYS == set(DEFS["response"]["required"]) == DEFS["response"]["properties"].keys()


def test_new_tool_names_do_not_collide_with_reserved_spellings():
    assert not set(EXEC_TOOLS) & set(RESERVED_TOOLS)
    assert not set(EXEC_FUNCTIONS) & set(RESERVED_FUNCTIONS)
    assert not set(EXEC_TOOLS.values()) & set(RESERVED_TOOLS.values())


@pytest.mark.parametrize("args", [
    {"target_kind": "pod", "target_name": "web", "lines": None},
    {"target_kind": "deployment", "target_name": "web-1", "lines": None},
    {"target_kind": "events", "target_name": None, "lines": None},
    {"target_kind": "events", "target_name": "web", "lines": None},
    {"target_kind": "logs", "target_name": "web", "lines": 1},
    {"target_kind": "logs", "target_name": "web", "lines": 200},
    {"target_kind": "logs", "target_name": "web", "lines": None},
    {"target_kind": "logs", "target_name": None, "lines": 5},
    {"target_kind": "pod", "target_name": None, "lines": None},
    {"target_kind": "pod", "target_name": "web", "lines": 5},
    {"target_kind": "events", "target_name": None, "lines": 5},
    {"target_kind": "logs", "target_name": "web", "lines": 0},
    {"target_kind": "logs", "target_name": "web", "lines": 201},
    {"target_kind": "logs", "target_name": "web", "lines": True},
    {"target_kind": "logs", "target_name": "web", "lines": "5"},
    {"target_kind": "pod", "target_name": "Web", "lines": None},
    {"target_kind": "pod", "target_name": "a" * 64, "lines": None},
    {"target_kind": "pod", "target_name": "", "lines": None},
    # Kubernetes name rule (DNS label): no leading or trailing hyphen, so no kubectl option.
    {"target_kind": "pod", "target_name": "a", "lines": None},
    {"target_kind": "pod", "target_name": "0", "lines": None},
    {"target_kind": "pod", "target_name": "a" * 63, "lines": None},
    {"target_kind": "pod", "target_name": "a--b", "lines": None},
    {"target_kind": "pod", "target_name": "-", "lines": None},
    {"target_kind": "pod", "target_name": "--all-namespaces", "lines": None},
    {"target_kind": "events", "target_name": "-A", "lines": None},
    {"target_kind": "logs", "target_name": "-web", "lines": 5},
    {"target_kind": "deployment", "target_name": "web-", "lines": None},
    {"target_kind": "pod", "target_name": "a" * 62 + "-", "lines": None},
    {"target_kind": "service", "target_name": "web", "lines": None},
    {"target_kind": "pod", "target_name": "web"},
    {"target_kind": "pod", "target_name": "web", "lines": None, "namespace": "other"},
])
def test_inspect_validation_matches_the_contract_schema(args):
    try:
        inspect_arguments(args)
        ours = True
    except ValueError:
        ours = False
    assert ours == INSPECT.is_valid(args)


@pytest.mark.parametrize("args", [
    {"service": "web", "port": 8080, "method": "TCP", "path": None},
    {"service": "web", "port": 1, "method": "HTTP_GET", "path": "/"},
    {"service": "web", "port": 65535, "method": "HTTP_HEAD", "path": "/health?x=1&y=%20"},
    {"service": "web", "port": 80, "method": "HTTP_GET", "path": "/" + "a" * 255},
    {"service": "web", "port": 80, "method": "HTTP_GET", "path": "/" + "a" * 256},
    {"service": "web", "port": 80, "method": "TCP", "path": "/"},
    {"service": "web", "port": 80, "method": "HTTP_GET", "path": None},
    {"service": "web", "port": 0, "method": "TCP", "path": None},
    {"service": "web", "port": 65536, "method": "TCP", "path": None},
    {"service": "web", "port": "80", "method": "TCP", "path": None},
    {"service": "web", "port": 80, "method": "UDP", "path": None},
    {"service": "web", "port": 80, "method": "HTTP_GET", "path": "health"},
    {"service": "web", "port": 80, "method": "HTTP_GET", "path": "/a b"},
    {"service": "web.other.svc", "port": 80, "method": "TCP", "path": None},
    {"service": "a", "port": 80, "method": "TCP", "path": None},
    {"service": "a" * 63, "port": 80, "method": "TCP", "path": None},
    {"service": "a" * 64, "port": 80, "method": "TCP", "path": None},
    {"service": "my-svc-1", "port": 80, "method": "TCP", "path": None},
    {"service": "--all-namespaces", "port": 80, "method": "TCP", "path": None},
    {"service": "-web", "port": 80, "method": "HTTP_GET", "path": "/"},
    {"service": "web-", "port": 80, "method": "TCP", "path": None},
    {"service": "-", "port": 80, "method": "TCP", "path": None},
    {"service": "web", "port": 80, "method": "TCP"},
    {"service": "web", "port": 80, "method": "TCP", "path": None, "host": "example.com"},
])
def test_probe_validation_matches_the_contract_schema(args):
    try:
        probe_arguments(args)
        ours = True
    except ValueError:
        ours = False
    assert ours == PROBE.is_valid(args)


@pytest.mark.parametrize("validate,args", [
    # Stricter than the Python jsonschema reading of the file: 5.0 is not an integer argument, and
    # ``$`` does not admit a trailing newline (ECMA-262 semantics, which the contract pattern means).
    (inspect_arguments, {"target_kind": "logs", "target_name": "web", "lines": 5.0}),
    (inspect_arguments, {"target_kind": "pod", "target_name": "web\n", "lines": None}),
    (probe_arguments, {"service": "web\n", "port": 80, "method": "TCP", "path": None}),
    (probe_arguments, {"service": "web", "port": 80.0, "method": "TCP", "path": None}),
])
def test_known_stricter_cases(validate, args):
    with pytest.raises(ValueError):
        validate(args)


@pytest.mark.parametrize("name", ["--all-namespaces", "-A", "-n", "-web", "web-", "-"])
def test_option_shaped_names_are_rejected_by_t11_and_the_contract(context, name):
    inspect = {"target_kind": "pod", "target_name": name, "lines": None}
    probe = {"service": name, "port": 80, "method": "TCP", "path": None}
    assert not INSPECT.is_valid(inspect) and not PROBE.is_valid(probe)
    for validate, args in ((inspect_arguments, inspect), (probe_arguments, probe)):
        with pytest.raises(ValueError):
            validate(args)
    channel = FakeChannel()
    tools = open_tools(context, channel)
    for tool, args in (("mcp__bravo__rehearsal_exec_inspect", inspect), ("mcp__bravo__rehearsal_exec_probe", probe)):
        with pytest.raises(PolicyDenied):
            tools.dispatch(tool, args)
        assert tools.events[-1]["reason"] == "INVALID_INPUT_OR_POLICY"
    assert not channel.sent


def response_cases():
    apply = exec_envelope("mcp__bravo__rehearsal_exec_apply", {"any": "proposal"})
    inspect = exec_envelope("mcp__bravo__rehearsal_exec_inspect", INSPECT_OK)
    probe = exec_envelope("mcp__bravo__rehearsal_exec_probe", PROBE_OK)
    reject = {"result_code": "REJECTED", "reason": "BLOCKED", "stage": "VALIDATE", "status": None, "output_tail": None}
    cases = [
        (apply, ok_response(apply)), (inspect, ok_response(inspect)), (probe, ok_response(probe)),
        (probe, ok_response(probe, status=ok_response(probe)["status"] | {"http_status": None, "probe_outcome": "REFUSED"})),
        (inspect, ok_response(inspect, reason="OUTPUT_WITHHELD", stage="OUTPUT", output_tail=None)),
        (inspect, ok_response(inspect, **reject)),
        (apply, ok_response(apply, **reject | {"reason": "FOREIGN_ATTEMPT", "stage": "ADMIT"})),
        (apply, ok_response(apply, result_code="FAILED", reason="NONZERO_EXIT", stage="EXECUTE", status=None,
                            output_tail="admission webhook denied", output_truncated=True)),
        (probe, ok_response(probe, result_code="FAILED", reason="TIMEOUT", stage="EXECUTE", status=None)),
        (apply, ok_response(apply, result_code="UNKNOWN", reason="EFFECT_UNKNOWN", stage="EXECUTE", status=None,
                            output_tail=None)),
        # invalid
        (inspect, ok_response(inspect, reason="BLOCKED", stage="ADMIT")),
        (inspect, ok_response(inspect, status=None)),
        (inspect, ok_response(inspect, **reject | {"status": ok_response(inspect)["status"]})),
        (inspect, ok_response(inspect, **reject | {"output_tail": "x"})),
        (inspect, ok_response(inspect, **reject | {"reason": "TIMEOUT"})),
        (apply, ok_response(apply, result_code="UNKNOWN", reason="UNAVAILABLE", stage="EXECUTE", status=None,
                            output_tail=None)),
        (inspect, ok_response(inspect, stage="ADMIT")),
        (inspect, ok_response(inspect, **reject | {"stage": None})),
        (inspect, ok_response(inspect, reason="OUTPUT_WITHHELD", stage="OUTPUT")),
        (inspect, ok_response(inspect, reason="OUTPUT_WITHHELD", stage="OUTPUT", output_tail=None, output_truncated=True)),
        (inspect, {k: v for k, v in ok_response(inspect).items() if k != "budget"}),
        (inspect, ok_response(inspect, extra=1)),
        (inspect, ok_response(inspect, schema_version=2)),
        (inspect, ok_response(inspect, budget={"calls_left": 6, "seconds_left": 1})),
        (inspect, ok_response(inspect, budget={"calls_left": 1, "seconds_left": 91})),
        (inspect, ok_response(inspect, status=ok_response(apply)["status"])),
        (probe, ok_response(probe, status=ok_response(probe)["status"] | {"http_status": 99})),
        (probe, ok_response(probe, status=ok_response(probe)["status"] | {"elapsed_ms": 15001})),
        (apply, ok_response(apply, status=ok_response(apply)["status"] | {"targets": [{"kind": "pod", "name": "a"}] * 9})),
        (apply, ok_response(apply, status=ok_response(apply)["status"] | {"targets": [{"kind": "secret", "name": "a"}]})),
        (apply, ok_response(apply, status=ok_response(apply)["status"] | {"targets": [{"kind": "pod", "name": "-web"}]})),
        (apply, ok_response(apply, status=ok_response(apply)["status"] | {"targets": [{"kind": "pod", "name": "web-"}]})),
        (inspect, ok_response(inspect, status=ok_response(inspect)["status"] | {"phase": "Evicted"})),
        (inspect, ok_response(inspect, output_tail="a" * 2049)),
        (inspect, ok_response(inspect, output_truncated="no")),
    ]
    return cases


@pytest.mark.parametrize("index", range(len(response_cases())))
def test_response_check_matches_the_contract_schema(index):
    envelope, response = response_cases()[index]
    try:
        check_response(response, envelope)
        ours = True
    except ValueError:
        ours = False
    assert ours == RESPONSE.is_valid(response)


def test_response_check_adds_binding_and_byte_limits():
    envelope = exec_envelope("mcp__bravo__rehearsal_exec_apply", {"any": "proposal"})
    for response in (ok_response(envelope, request_id="t11-" + "0" * 32),
                     ok_response(envelope, action="probe", status=ok_response(
                         exec_envelope("mcp__bravo__rehearsal_exec_probe", PROBE_OK))["status"]),
                     ok_response(envelope, status=ok_response(envelope)["status"] | {"proposal_digest": "b" * 64}),
                     # 2 KiB is UTF-8 bytes (contract 5); the schema's maxLength counts characters.
                     ok_response(envelope, output_tail="한" * 1000)):
        with pytest.raises(ValueError):
            check_response(response, envelope)
    assert RESPONSE.is_valid(ok_response(envelope, output_tail="한" * 1000))


@pytest.mark.parametrize("code,reason,stage", [("UNKNOWN", "EFFECT_UNKNOWN", "EXECUTE"),
                                               ("FAILED", "UNAVAILABLE", "EXECUTE"),
                                               ("REJECTED", "UNAVAILABLE", "ADMIT"),
                                               ("REJECTED", "EFFECT_UNKNOWN", "ADMIT"),
                                               ("REJECTED", "BUDGET_EXHAUSTED", "ADMIT")])
def test_t11_generated_responses_are_contract_responses(code, reason, stage):
    envelope = exec_envelope("mcp__bravo__rehearsal_exec_probe", PROBE_OK)
    response = local_response(envelope, code, reason, stage)
    assert RESPONSE.is_valid(response) and check_response(response, envelope) is response


# --- Tools: exposure, validation, channel failures ------------------------------------------------

NOT_TRUE = [{}, {"exec_enabled": False}, {"exec_enabled": None}, {"exec_enabled": 1},
            {"exec_enabled": "true"}, {"exec_enabled": "True"}, {"exec_enabled": [True]}]


@pytest.mark.parametrize("enabled", NOT_TRUE + [{"exec_enabled": True}])
@pytest.mark.parametrize("channel", [None, False, "yes", True])
def test_exec_opens_only_with_exec_enabled_true_and_an_enabled_channel(context, enabled, channel):
    bundle, snapshot, _, _ = context
    channel = None if channel is None else FakeChannel(enabled=channel)
    tools = Tools(bundle, snapshot, 1, exec_channel=channel, **enabled)
    tools.set_model_budget(7, time.monotonic() + 180)
    opened = enabled.get("exec_enabled") is True and channel is not None and channel.enabled is True
    assert tools.exec_active is opened and tools.allowed("mcp__bravo__rehearsal_exec_probe") is opened
    names = {t["name"] for t in function_tools(tools)}
    assert (set(EXEC_FUNCTIONS) <= names) is opened and (not set(EXEC_FUNCTIONS) & names) is not opened
    if opened:
        assert tools.dispatch("mcp__bravo__rehearsal_exec_probe", PROBE_OK)["result_code"] == "OK"
        return
    for name, args in (("mcp__bravo__rehearsal_exec_probe", PROBE_OK), ("mcp__bravo__rehearsal_exec_inspect", INSPECT_OK)):
        with pytest.raises(PolicyDenied):
            tools.dispatch(name, args)
        assert tools.events[-1] == {"run_id": "run-1", "attempt": 1, "tool": EXEC_TOOLS[name],
                                    "outcome": "DENIED", "reason": "TOOL_NOT_PROVIDED"}
    assert channel is None or not channel.sent
    assert tools.system_prompt() == exec_tools_system(False)


@pytest.mark.parametrize("enabled", NOT_TRUE)
def test_coordinator_with_a_channel_but_exec_not_enabled_keeps_todays_behaviour(context, monkeypatch, enabled):
    bundle, snapshot, proposal, _ = context
    channel = FakeChannel()
    requests = install_sdk(monkeypatch, [call("rehearsal_exec_probe", PROBE_OK), call("plan_propose", proposal)])
    coordinator = Coordinator(bundle, snapshot, exec_channel=channel, **enabled)
    result = asyncio.run(coordinator.propose(OpenAIProvider("fixture-model")))
    assert result["state"] == "PROPOSAL_READY" and not channel.sent
    assert result["audit"][0] == {"run_id": "run-1", "attempt": 1, "tool": "rehearsal.exec.probe",
                                  "outcome": "DENIED", "reason": "TOOL_NOT_PROVIDED"}
    assert {t["name"] for r in requests for t in r["tools"]} == {"repo_read", "plan_propose", "policy_evaluate"}
    assert "rehearsal_exec" not in requests[0]["instructions"]


def test_without_an_enabled_channel_exec_names_stay_not_provided(context):
    bundle, snapshot, _, _ = context
    for channel in (None, FakeChannel(enabled=False), FakeChannel(enabled="yes")):
        tools = Tools(bundle, snapshot, 1, exec_channel=channel, exec_enabled=True)
        tools.set_model_budget(7, time.monotonic() + 180)
        assert not tools.exec_active and not tools.allowed("mcp__bravo__rehearsal_exec_probe")
        assert {t["name"] for t in function_tools(tools)} == {"repo_read", "plan_propose", "policy_evaluate"}
        with pytest.raises(PolicyDenied):
            tools.dispatch("mcp__bravo__rehearsal_exec_probe", PROBE_OK)
        assert tools.events[-1] == {"run_id": "run-1", "attempt": 1, "tool": "rehearsal.exec.probe",
                                    "outcome": "DENIED", "reason": "TOOL_NOT_PROVIDED"}
        assert tools.system_prompt() == exec_tools_system(False)


def exec_tools_system(active):
    from bravo_agent.tools import EXEC_SYSTEM, SYSTEM
    return SYSTEM + EXEC_SYSTEM if active else SYSTEM


def test_envelope_is_a_contract_local_request_with_a_fresh_request_id(context):
    channel = FakeChannel()
    tools = open_tools(context, channel)
    _, _, proposal, _ = context
    tools.dispatch("mcp__bravo__rehearsal_exec_inspect", INSPECT_OK)
    tools.dispatch("mcp__bravo__rehearsal_exec_inspect", INSPECT_OK)
    tools.dispatch("mcp__bravo__rehearsal_exec_probe", PROBE_OK)
    tools.dispatch("mcp__bravo__rehearsal_exec_apply", proposal)
    assert len({envelope["request_id"] for envelope in channel.sent}) == 4
    for envelope in channel.sent:
        assert LOCAL_REQUEST.is_valid(envelope), list(LOCAL_REQUEST.iter_errors(envelope))
        assert envelope["schema_version"] == 1 and envelope["args_sha256"] == digest(envelope["arguments"])
        assert not {"run_id", "attempt", "namespace"} & envelope.keys()
    apply = channel.sent[-1]
    assert apply["tool"] == "rehearsal.exec.apply" and apply["arguments"]["schema_version"] == 1
    # apply is checked like plan.propose but is not the attempt's single submission.
    assert tools.proposal is None
    tools.dispatch("mcp__bravo__plan_propose", proposal)
    assert tools.proposal is not None
    for _ in range(2):
        tools.dispatch("mcp__bravo__rehearsal_exec_apply", proposal)  # counted only by T15's exec limit
    assert len(channel.sent) == 6


def test_exec_audit_has_request_and_result_but_no_argument_values(context):
    channel = FakeChannel()
    tools = open_tools(context, channel)
    response = tools.dispatch("mcp__bravo__rehearsal_exec_probe", PROBE_OK)
    assert response["result_code"] == "OK" and response["status"]["http_status"] == 403
    assert tools.events[-1] == {"run_id": "run-1", "attempt": 1, "tool": "rehearsal.exec.probe", "outcome": "ALLOWED",
                                "reason": "POLICY_ACCEPTED", "request_id": channel.sent[0]["request_id"],
                                "result_code": "OK"}
    assert "private" not in json.dumps(tools.events)
    # collect_tool_events (T15) signs only str/int/bool values.
    assert all(type(value) in (str, int, bool) for event in tools.events for value in event.values())


@pytest.mark.parametrize("tool,args,reason,category", [
    ("mcp__bravo__rehearsal_exec_inspect", {"target_kind": "events", "target_name": None, "lines": 5},
     "INVALID_INPUT_OR_POLICY", None),
    ("mcp__bravo__rehearsal_exec_probe", {"service": "web", "port": 80, "method": "TCP", "path": "/"},
     "INVALID_INPUT_OR_POLICY", None),
    ("mcp__bravo__rehearsal_exec_inspect", {"target_kind": "pod", "target_name": "rm -rf /", "lines": None},
     "DESTRUCTIVE_ATTEMPT_BLOCKED", "filesystem_rm"),
    ("mcp__bravo__rehearsal_exec_probe", "kubectl delete namespace private-ns", "DESTRUCTIVE_ATTEMPT_BLOCKED",
     "namespace_delete"),
    ("mcp__bravo__rehearsal_exec_inspect", ["not", "an", "object"], "INVALID_INPUT_OR_POLICY", None),
])
def test_invalid_or_destructive_exec_calls_are_denied_before_the_channel(context, tool, args, reason, category):
    channel = FakeChannel()
    tools = open_tools(context, channel)
    with pytest.raises(PolicyDenied):
        tools.dispatch(tool, args)
    assert not channel.sent
    assert tools.events[-1]["outcome"] == "DENIED" and tools.events[-1]["reason"] == reason
    assert tools.events[-1].get("category") == category
    assert "private-ns" not in json.dumps(tools.events)


@pytest.mark.parametrize("change,reason,category", [
    (lambda p: p["edits"][0].update(path="src/other.py"), "INVALID_INPUT_OR_POLICY", None),
    (lambda p: p["edits"][0].update(before_sha256="0" * 64), "INVALID_INPUT_OR_POLICY", None),
    (lambda p: p.update(run_id="run-2"), "INVALID_INPUT_OR_POLICY", None),
    (lambda p: p["edits"][0].update(replacement="aws s3 rb s3://private-bucket --force\n"),
     "DESTRUCTIVE_ATTEMPT_BLOCKED", "bucket_delete"),
    # plan.propose accepts this rationale; an exec call refuses any destructive pattern.
    (lambda p: p.update(rationale="Then DROP the old table"), "DESTRUCTIVE_ATTEMPT_BLOCKED", "db_drop"),
])
def test_apply_uses_the_plan_propose_checks(context, change, reason, category):
    _, _, proposal, _ = context
    proposal = copy.deepcopy(proposal)
    change(proposal)
    channel = FakeChannel()
    tools = open_tools(context, channel)
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__rehearsal_exec_apply", proposal)
    assert not channel.sent and tools.events[-1]["reason"] == reason and tools.events[-1].get("category") == category


def failing(_envelope):
    raise TimeoutError("private channel detail")


def test_apply_lost_in_the_channel_is_effect_unknown_and_closes_exec(context):
    _, _, proposal, _ = context
    channel = FakeChannel(reply=failing)
    tools = open_tools(context, channel)
    first = tools.dispatch("mcp__bravo__rehearsal_exec_apply", proposal)
    assert (first["result_code"], first["reason"], first["stage"]) == ("UNKNOWN", "EFFECT_UNKNOWN", "EXECUTE")
    channel.reply = ok_response
    later = tools.dispatch("mcp__bravo__rehearsal_exec_inspect", INSPECT_OK)
    assert (later["result_code"], later["reason"], later["stage"]) == ("REJECTED", "EFFECT_UNKNOWN", "ADMIT")
    assert len(channel.sent) == 1  # never re-sent
    assert tools.events[-2]["result_code"] == "UNKNOWN" and tools.events[-2]["exec_reason"] == "EFFECT_UNKNOWN"
    assert tools.events[-1] == {"run_id": "run-1", "attempt": 1, "tool": "rehearsal.exec.inspect", "outcome": "DENIED",
                                "reason": "EXEC_CLOSED", "request_id": later["request_id"], "result_code": "REJECTED",
                                "exec_reason": "EFFECT_UNKNOWN"}
    assert all(RESPONSE.is_valid(r) for r in (first, later)) and "private" not in json.dumps(tools.events)
    # Submission still works after an unknown effect (contract 3, ADR-0026 3-3).
    tools.dispatch("mcp__bravo__plan_propose", proposal)
    assert tools.proposal is not None


@pytest.mark.parametrize("reply", [failing, lambda e: {"not": "a response"},
                                   lambda e: ok_response(e, request_id="t11-" + "f" * 32)])
def test_inspect_or_probe_channel_failure_is_unavailable_then_rejected(context, reply):
    channel = FakeChannel(reply=reply)
    tools = open_tools(context, channel)
    first = tools.dispatch("mcp__bravo__rehearsal_exec_probe", PROBE_OK)
    assert (first["result_code"], first["reason"], first["stage"]) == ("FAILED", "UNAVAILABLE", "EXECUTE")
    channel.reply = ok_response
    later = tools.dispatch("mcp__bravo__rehearsal_exec_inspect", INSPECT_OK)
    assert (later["result_code"], later["reason"], later["stage"]) == ("REJECTED", "UNAVAILABLE", "ADMIT")
    assert len(channel.sent) == 1


def test_invalid_apply_response_is_effect_unknown(context):
    _, _, proposal, _ = context
    channel = FakeChannel(reply=lambda e: ok_response(e, status=ok_response(e)["status"] | {"proposal_digest": "b" * 64}))
    tools = open_tools(context, channel)
    assert tools.dispatch("mcp__bravo__rehearsal_exec_apply", proposal)["result_code"] == "UNKNOWN"
    assert tools.dispatch("mcp__bravo__rehearsal_exec_probe", PROBE_OK)["reason"] == "EFFECT_UNKNOWN"


def test_effect_unknown_reported_by_t15_also_closes_exec_locally(context):
    _, _, proposal, _ = context
    channel = FakeChannel(reply=lambda e: ok_response(e, result_code="UNKNOWN", reason="EFFECT_UNKNOWN",
                                                      stage="EXECUTE", status=None, output_tail=None))
    tools = open_tools(context, channel)
    tools.dispatch("mcp__bravo__rehearsal_exec_apply", proposal)
    assert tools.dispatch("mcp__bravo__rehearsal_exec_inspect", INSPECT_OK)["stage"] == "ADMIT"
    assert len(channel.sent) == 1


def test_channel_disabled_mid_attempt_is_unavailable_without_sending(context):
    channel = FakeChannel()
    tools = open_tools(context, channel)
    channel.enabled = False
    response = tools.dispatch("mcp__bravo__rehearsal_exec_inspect", INSPECT_OK)
    assert (response["result_code"], response["reason"], response["stage"]) == ("REJECTED", "UNAVAILABLE", "ADMIT")
    assert not channel.sent


def test_secret_shaped_output_is_withheld_from_the_model(context):
    channel = FakeChannel(reply=lambda e: ok_response(e, output_tail="key AKIA" + "A" * 16, output_truncated=True))
    tools = open_tools(context, channel)
    response = tools.dispatch("mcp__bravo__rehearsal_exec_inspect", INSPECT_OK)
    assert response["output_tail"] is None and response["output_truncated"] is False
    assert (response["reason"], response["stage"]) == ("OUTPUT_WITHHELD", "OUTPUT") and RESPONSE.is_valid(response)
    assert tools.events[-1]["exec_reason"] == "OUTPUT_WITHHELD"


@pytest.mark.parametrize("turns_left,seconds,reserve,open_", [
    (7, 180, ExecReserve(), True), (2, 180, ExecReserve(), True), (1, 180, ExecReserve(), False),
    (7, 44, ExecReserve(), False), (7, 180, ExecReserve(turns=8), False), (None, 180, ExecReserve(), False),
])
def test_budget_guard_keeps_room_for_plan_propose(context, turns_left, seconds, reserve, open_):
    bundle, snapshot, _, _ = context
    channel = FakeChannel()
    tools = Tools(bundle, snapshot, 1, exec_channel=channel, exec_reserve=reserve, exec_enabled=True)
    if turns_left is not None:
        tools.set_model_budget(turns_left, time.monotonic() + seconds)
    assert tools.exec_open() is open_
    response = tools.dispatch("mcp__bravo__rehearsal_exec_inspect", INSPECT_OK)
    if open_:
        assert response["result_code"] == "OK" and len(channel.sent) == 1
    else:
        assert (response["result_code"], response["reason"], response["stage"]) == ("REJECTED", "BUDGET_EXHAUSTED", "ADMIT")
        assert not channel.sent and response["budget"] == {"calls_left": 0, "seconds_left": 0}


# --- the real OpenAI provider over a mock HTTP transport -------------------------------------------

def call(name, arguments):
    return {"name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}


def test_openai_path_runs_exec_then_submits_once(context, monkeypatch):
    bundle, snapshot, proposal, _ = context
    channel = FakeChannel()
    requests = install_sdk(monkeypatch, [
        call("rehearsal_exec_apply", proposal), call("rehearsal_exec_inspect", INSPECT_OK),
        call("rehearsal_exec_probe", PROBE_OK), call("plan_propose", proposal)])
    result = asyncio.run(Coordinator(bundle, snapshot, exec_channel=channel, exec_enabled=True).propose(OpenAIProvider("fixture-model")))
    assert result["state"] == "PROPOSAL_READY" and len(requests) == 4
    offered = {tool["name"]: tool for tool in requests[0]["tools"]}
    assert set(offered) == {"repo_read", "plan_propose", "policy_evaluate", *EXEC_FUNCTIONS}
    assert all(tool["strict"] for tool in offered.values())
    assert offered["rehearsal_exec_inspect"]["parameters"] == INSPECT_PARAMETERS
    assert offered["rehearsal_exec_probe"]["parameters"] == PROBE_PARAMETERS
    assert offered["rehearsal_exec_apply"]["parameters"] == strict_schema(Proposal.model_json_schema())
    assert "rehearsal_exec_apply" in requests[0]["instructions"]
    assert [envelope["tool"] for envelope in channel.sent] == [
        "rehearsal.exec.apply", "rehearsal.exec.inspect", "rehearsal.exec.probe"]
    outputs = [json.loads(r["input"][-1]["output"]) for r in requests[1:]]
    assert [o["request_id"] for o in outputs] == [e["request_id"] for e in channel.sent]
    assert all(RESPONSE.is_valid(o) for o in outputs)
    exec_events = [e for e in result["audit"] if e["tool"].startswith("rehearsal.exec.")]
    assert [(e["tool"], e["outcome"], e["result_code"]) for e in exec_events] == [
        ("rehearsal.exec.apply", "ALLOWED", "OK"), ("rehearsal.exec.inspect", "ALLOWED", "OK"),
        ("rehearsal.exec.probe", "ALLOWED", "OK")]
    assert result["proposal_digest"] == digest(Proposal.model_validate(proposal)) == channel.sent[0]["args_sha256"]
    assert "private" not in json.dumps(result["audit"])


def test_openai_path_without_channel_keeps_todays_behaviour(context, monkeypatch):
    bundle, snapshot, proposal, _ = context
    requests = install_sdk(monkeypatch, [call("rehearsal_exec_apply", proposal), call("plan_propose", proposal)])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model")))
    assert result["state"] == "PROPOSAL_READY"
    assert result["audit"][0] == {"run_id": "run-1", "attempt": 1, "tool": "rehearsal.exec.apply",
                                  "outcome": "DENIED", "reason": "TOOL_NOT_PROVIDED"}
    assert {t["name"] for r in requests for t in r["tools"]} == {"repo_read", "plan_propose", "policy_evaluate"}
    assert "rehearsal_exec" not in requests[0]["instructions"]


@pytest.mark.parametrize("name", ["rehearsal_exec", "rehearsal.exec", "bravo.rehearsal_exec", "rehearsal_run"])
def test_openai_reserved_names_stay_not_provided_with_a_channel(context, monkeypatch, name):
    bundle, snapshot, proposal, _ = context
    channel = FakeChannel()
    install_sdk(monkeypatch, [call(name, {"command": "ls"}), call("plan_propose", proposal)])
    result = asyncio.run(Coordinator(bundle, snapshot, exec_channel=channel, exec_enabled=True).propose(OpenAIProvider("fixture-model")))
    assert result["audit"][0]["reason"] == "TOOL_NOT_PROVIDED" and not channel.sent


def test_openai_path_closes_exec_when_only_the_submission_turns_remain(context, monkeypatch):
    bundle, snapshot, proposal, _ = context
    channel = FakeChannel()
    requests = install_sdk(monkeypatch, [call("rehearsal_exec_inspect", INSPECT_OK),
                                         call("rehearsal_exec_inspect", INSPECT_OK), call("plan_propose", proposal)])
    coordinator = Coordinator(bundle, snapshot, exec_channel=channel, exec_reserve=ExecReserve(turns=7), exec_enabled=True)
    result = asyncio.run(coordinator.propose(OpenAIProvider("fixture-model")))
    assert result["state"] == "PROPOSAL_READY"
    names = [{t["name"] for t in r["tools"]} for r in requests]
    assert "rehearsal_exec_inspect" in names[0] and not names[1] & set(EXEC_FUNCTIONS)
    assert len(channel.sent) == 1
    second = json.loads(requests[2]["input"][-1]["output"])
    assert (second["result_code"], second["reason"], second["stage"]) == ("REJECTED", "BUDGET_EXHAUSTED", "ADMIT")


def test_openai_path_classifies_unparseable_exec_arguments(context, monkeypatch):
    bundle, snapshot, proposal, _ = context
    channel = FakeChannel()
    install_sdk(monkeypatch, [call("rehearsal_exec_probe", '{"service": "aws s3 rb s3://private-b'),
                              call("plan_propose", proposal)])
    result = asyncio.run(Coordinator(bundle, snapshot, exec_channel=channel, exec_enabled=True).propose(OpenAIProvider("fixture-model")))
    assert result["audit"][0] == {"run_id": "run-1", "attempt": 1, "tool": "rehearsal.exec.probe", "outcome": "DENIED",
                                  "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED", "category": "bucket_delete"}
    assert not channel.sent and "private-b" not in json.dumps(result)


# --- the Claude SDK hook and MCP handler path -------------------------------------------------------

def captured_handlers(monkeypatch):
    from bravo_agent import sdk

    captured = {}
    real = sdk.create_sdk_mcp_server

    def capture(name, tools):
        captured.update({handler.name: handler for handler in tools})
        return real(name=name, tools=tools)

    monkeypatch.setattr(sdk, "create_sdk_mcp_server", capture)
    return captured


def test_claude_path_exposes_exec_only_with_a_channel(context, tmp_path, monkeypatch):
    from bravo_agent.sdk import guard, make_options

    bundle, snapshot, _, _ = context
    handlers = captured_handlers(monkeypatch)
    off = Tools(bundle, snapshot, 1)
    options = make_options(off, tmp_path, "explicit-model")
    assert not set(EXEC_TOOLS) & set(options.allowed_tools) and not set(EXEC_FUNCTIONS) & set(handlers)
    # A channel without the run's exec_enabled=True is still off (contract 1, default off).
    for half in (Tools(bundle, snapshot, 1, exec_channel=FakeChannel()),
                 Tools(bundle, snapshot, 1, exec_channel=FakeChannel(), exec_enabled="true")):
        options = make_options(half, tmp_path, "explicit-model")
        assert not set(EXEC_TOOLS) & set(options.allowed_tools) and not set(EXEC_FUNCTIONS) & set(handlers)
        assert "rehearsal_exec" not in options.system_prompt
    denied = asyncio.run(guard(off, {"tool_name": "mcp__bravo__rehearsal_exec_apply", "tool_input": {}}, None, None))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert off.events[-1]["reason"] == "TOOL_NOT_PROVIDED" and off.events[-1]["tool"] == "rehearsal.exec.apply"

    on = open_tools(context, FakeChannel())
    options = make_options(on, tmp_path, "explicit-model")
    assert set(EXEC_TOOLS) <= set(options.allowed_tools) and set(EXEC_FUNCTIONS) <= set(handlers)
    assert handlers["rehearsal_exec_inspect"].input_schema == INSPECT_PARAMETERS
    assert "rehearsal_exec_apply" in options.system_prompt
    for name in EXEC_TOOLS:
        allowed = asyncio.run(guard(on, {"tool_name": name, "tool_input": {}}, None, None))
        assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_claude_provider_runs_exec_through_hook_and_mcp_handler(context, tmp_path, monkeypatch):
    from bravo_agent import sdk

    bundle, snapshot, proposal, _ = context
    handlers = captured_handlers(monkeypatch)
    monkeypatch.setattr(sdk.os, "environ", {"ANTHROPIC_API_KEY": "model-fixture"})
    channel = FakeChannel()
    seen = []

    class Client:
        def __init__(self, **kwargs):
            self.options = kwargs["options"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            hook = self.options.hooks["PreToolUse"][0].hooks[0]
            for name, args in (("rehearsal_exec_probe", PROBE_OK), ("rehearsal_exec_inspect", INSPECT_OK),
                               ("rehearsal_exec_inspect", INSPECT_OK), ("plan_propose", proposal)):
                decision = await hook({"tool_name": "mcp__bravo__" + name, "tool_input": args}, None, None)
                assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"
                reply = await handlers[name].handler(args)
                seen.append(json.loads(reply["content"][0]["text"]))

        async def receive_response(self):
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="fixture")

    monkeypatch.setattr(sdk, "ClaudeSDKClient", Client)
    # Six of eight turns reserved: after two calls the third exec call is closed for the budget.
    coordinator = Coordinator(bundle, snapshot, exec_channel=channel, exec_reserve=ExecReserve(turns=6),
                              exec_enabled=True)
    result = asyncio.run(coordinator.propose(sdk.ClaudeProvider(tmp_path, "explicit-model")))
    assert result["state"] == "PROPOSAL_READY"
    assert [r.get("result_code") for r in seen[:3]] == ["OK", "OK", "REJECTED"]
    assert seen[2]["reason"] == "BUDGET_EXHAUSTED" and len(channel.sent) == 2
    assert seen[3]["status"] == "PROPOSAL_RECEIVED"
    assert "private" not in json.dumps(result["audit"])


def test_subscription_stream_path_namespaces_exec_tools(context, monkeypatch):
    from test_subscription_provider import completed, install_stream

    bundle, snapshot, _, _ = context
    channel = FakeChannel()
    requests = install_stream(monkeypatch, [[completed("repo_read", {"path": "src/config.py"})],
                                            [completed("rehearsal_exec_probe", PROBE_OK)],
                                            [completed("rehearsal_exec_inspect", INSPECT_OK, namespace="other")]])
    result = asyncio.run(Coordinator(bundle, snapshot, exec_channel=channel, exec_enabled=True).propose(
        OpenAIProvider("fixture-model", subscription=True)))
    # The first call is read-only; exec tools are then offered inside the bravo namespace only.
    assert [t["name"] for t in requests[0]["tools"][0]["tools"]] == ["repo_read"]
    assert set(EXEC_FUNCTIONS) <= {t["name"] for t in requests[1]["tools"][0]["tools"]}
    # A call outside the namespace is never executed and still ends the attempt.
    assert [envelope["tool"] for envelope in channel.sent] == ["rehearsal.exec.probe"]
    assert result["state"] == "NEEDS_HUMAN"
