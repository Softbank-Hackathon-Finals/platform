"""T19 readiness observation from the approval host: the Executor call and its bounded failures."""

import json

import httpx
import pytest

from bravo_platform_events import readiness_observer
from bravo_platform_events.executor import ExecutorClient, ExecutorReadError
from bravo_platform_events.gate_readiness import POLICY

PAIR = {
    "source": {"environment_id": "env-aws", "resource_id": "fixture/source", "connection_ref": "cutover/source"},
    "target": {"environment_id": "env-onprem", "resource_id": "fixture/target", "connection_ref": "cutover/target"},
}
OBSERVATION = {"kind": "t13-readiness-observation.v1"}
# What the Executor returns for PAIR: each DB named by its registered connection ref
NAMED = {"kind": "t13-readiness-observation.v1",
         "source": {"environment_id": "env-aws", "database_ref": "cutover/source"},
         "target": {"environment_id": "env-onprem", "database_ref": "cutover/target"},
         "replication": {"source_database_ref": "cutover/source", "target_database_ref": "cutover/target"}}


def client(handle, command_token="command-token"):
    return ExecutorClient("https://executor.invalid", token="read-token", command_token=command_token,
                          transport=httpx.MockTransport(handle))


def test_the_request_is_the_registered_pair_and_the_server_policy():
    seen = []

    def handle(request):
        seen.append((request.method, request.url.path, request.headers["Authorization"],
                     json.loads(request.content) if request.content else None))
        return httpx.Response(200, json=PAIR if request.method == "GET" else OBSERVATION)

    with client(handle) as executor:
        executor.verified_bindings(PAIR)
        assert executor.observe_readiness(readiness_observer.request_for(PAIR)) == OBSERVATION
    assert seen[0][:3] == ("GET", "/v1/resources", "Bearer read-token")
    method, path, auth, body = seen[1]
    assert (method, path, auth) == ("POST", "/v1/readiness", "Bearer command-token")
    assert body == {"source_environment_id": "env-aws", "target_environment_id": "env-onprem",
                    "tables": list(POLICY.tables), "sequence_columns": [["public.messages", "id"]],
                    "max_age_seconds": 30}


@pytest.mark.parametrize(("status", "detail", "code"), [
    (409, {"code": "execution_in_progress"}, "execution_in_progress"),
    (503, {"code": "readiness_capture_expired"}, "readiness_capture_expired"),
    (422, [{"loc": ["body"], "msg": "host=secret"}], "http_status"),
    (409, {"code": "host=secret-db password=x"}, "http_status"),
    (500, {"code": "execution_in_progress"}, "http_status"),
])
def test_refusals_pass_only_fixed_codes(status, detail, code):
    with client(lambda _: httpx.Response(status, json={"detail": detail})) as executor:
        with pytest.raises(ExecutorReadError) as raised:
            executor.observe_readiness(readiness_observer.request_for(PAIR))
    assert str(raised.value) == code


@pytest.mark.parametrize("answer", [{"error": "x"}, [], {"kind": "t13-readiness-observation.v1"}])
def test_a_refusal_without_a_fixed_code_is_an_http_status(answer):
    with client(lambda _: httpx.Response(503, json=answer)) as executor:
        with pytest.raises(ExecutorReadError, match="http_status"):
            executor.observe_readiness(readiness_observer.request_for(PAIR))


@pytest.mark.parametrize("answer", [{"kind": "something-else"}, {"error": "x"}, {}, [],
                                    {"detail": {"code": "execution_in_progress"}}])
def test_an_answer_that_is_not_an_observation_is_refused(answer):
    with client(lambda _: httpx.Response(200, json=answer)) as executor:
        with pytest.raises(ExecutorReadError, match="invalid_response"):
            executor.observe_readiness(readiness_observer.request_for(PAIR))


def test_no_command_credential_stops_before_network():
    with ExecutorClient("https://executor.invalid", token="read-token",
                        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request"))) as executor:
        with pytest.raises(ExecutorReadError, match="command_credential_missing"):
            executor.observe_readiness(readiness_observer.request_for(PAIR))


def operator_config(tmp_path, monkeypatch):
    for name, value in (("read", "read-token"), ("command", "command-token")):
        (tmp_path / name).write_text(value + "\n")
    (tmp_path / "ca.crt").write_text("")
    config = {"executor": {"url": "https://executor.invalid", "ca_file": str(tmp_path / "ca.crt"),
                           "token_file": str(tmp_path / "read"), "command_token_file": str(tmp_path / "command"),
                           "registered_pair": PAIR}, "gate": "module:factory"}
    (tmp_path / "operator.json").write_text(json.dumps(config))
    monkeypatch.setattr(readiness_observer.ssl, "create_default_context", lambda cafile: None)
    return config


def fake(remote, observation=NAMED):
    def make(url, *, token, command_token, tls_context, timeout_seconds):
        assert (token, command_token, timeout_seconds) == ("read-token", "command-token", 30)
        return client(lambda r: httpx.Response(200, json=remote if r.method == "GET" else observation))
    return make


def test_observe_reads_the_operator_config_and_checks_the_deployed_pair(tmp_path, monkeypatch):
    config = operator_config(tmp_path, monkeypatch)
    replaced = json.loads(json.dumps(PAIR))
    replaced["target"]["resource_id"] = "fixture/replaced"
    assert readiness_observer.observe(tmp_path / "operator.json", client=fake(PAIR)) == NAMED
    with pytest.raises(ExecutorReadError, match="registry_mismatch"):
        readiness_observer.observe(tmp_path / "operator.json", client=fake(replaced))
    config["executor"]["url"] = "http://executor.invalid"
    (tmp_path / "operator.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="operator_ports_configuration_invalid"):
        readiness_observer.observe(tmp_path / "operator.json", client=fake(PAIR))


def _named(change):
    observation = json.loads(json.dumps(NAMED))
    change(observation)
    return observation


@pytest.mark.parametrize("observation", [
    # the first Executor version: resource ids, which T10 refuses at SHIFT/cutover approval (#612)
    _named(lambda o: [o[s].update(database_ref=PAIR[s]["resource_id"]) for s in ("source", "target")]),
    _named(lambda o: o["replication"].update(target_database_ref="fixture/target")),
    _named(lambda o: o["source"].update(database_ref="cutover/target")),
    _named(lambda o: o["target"].update(environment_id="env-other")),
    _named(lambda o: o.pop("replication")),
    _named(lambda o: o.pop("source")),
    OBSERVATION,
])
def test_an_observation_not_named_by_the_registered_pair_holds_before_the_relay(tmp_path, monkeypatch,
                                                                               observation):
    """#612 follow-up: refuse here, before `capture` records and signs it for the context."""
    operator_config(tmp_path, monkeypatch)
    with pytest.raises(ExecutorReadError, match="^readiness_pair_mismatch$"):
        readiness_observer.observe(tmp_path / "operator.json", client=fake(PAIR, observation))


def test_main_prints_the_pair_mismatch_as_a_hold(monkeypatch, capsys):
    def refuse():
        raise ExecutorReadError("readiness_pair_mismatch")
    monkeypatch.setattr(readiness_observer, "observe", refuse)
    with pytest.raises(SystemExit) as stopped:
        readiness_observer.main()
    assert stopped.value.code == 10
    assert json.loads(capsys.readouterr().out) == {"status": "HOLD", "code": "readiness_pair_mismatch"}
