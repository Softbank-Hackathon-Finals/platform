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


@pytest.mark.parametrize("answer", [{"kind": "something-else"}, {"error": "x"}, {}])
def test_an_answer_that_is_not_an_observation_is_refused(answer):
    with client(lambda _: httpx.Response(200, json=answer)) as executor:
        with pytest.raises(ExecutorReadError, match="invalid_response"):
            executor.observe_readiness(readiness_observer.request_for(PAIR))


def test_no_command_credential_stops_before_network():
    with ExecutorClient("https://executor.invalid", token="read-token",
                        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request"))) as executor:
        with pytest.raises(ExecutorReadError, match="command_credential_missing"):
            executor.observe_readiness(readiness_observer.request_for(PAIR))


def test_observe_reads_the_operator_config_and_checks_the_deployed_pair(tmp_path, monkeypatch):
    for name, value in (("read", "read-token"), ("command", "command-token")):
        (tmp_path / name).write_text(value + "\n")
    (tmp_path / "ca.crt").write_text("")
    config = {"executor": {"url": "https://executor.invalid", "ca_file": str(tmp_path / "ca.crt"),
                           "token_file": str(tmp_path / "read"), "command_token_file": str(tmp_path / "command"),
                           "registered_pair": PAIR}, "gate": "module:factory"}
    (tmp_path / "operator.json").write_text(json.dumps(config))
    replaced = json.loads(json.dumps(PAIR))
    replaced["target"]["resource_id"] = "fixture/replaced"

    def fake(remote):
        def make(url, *, token, command_token, tls_context, timeout_seconds):
            assert (token, command_token, timeout_seconds) == ("read-token", "command-token", 30)
            return client(lambda r: httpx.Response(200, json=remote if r.method == "GET" else OBSERVATION))
        return make

    monkeypatch.setattr(readiness_observer.ssl, "create_default_context", lambda cafile: None)
    assert readiness_observer.observe(tmp_path / "operator.json", client=fake(PAIR)) == OBSERVATION
    with pytest.raises(ExecutorReadError, match="registry_mismatch"):
        readiness_observer.observe(tmp_path / "operator.json", client=fake(replaced))
    config["executor"]["url"] = "http://executor.invalid"
    (tmp_path / "operator.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="operator_ports_configuration_invalid"):
        readiness_observer.observe(tmp_path / "operator.json", client=fake(PAIR))
