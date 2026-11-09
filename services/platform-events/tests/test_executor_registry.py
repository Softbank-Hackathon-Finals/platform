import json
from copy import deepcopy

import httpx
import pytest

from bravo_platform_events.executor import ExecutorClient, ExecutorReadError
from bravo_platform_events.operator_access import create_operator_cutover_app

PAIR = {
    "source": {"environment_id": "env-aws", "resource_id": "fixture/source",
               "connection_ref": "cutover/source"},
    "target": {"environment_id": "env-onprem", "resource_id": "fixture/target",
               "connection_ref": "cutover/target"},
}


def test_host_accepts_only_an_independently_enrolled_pair():
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer fixture-token"
        return httpx.Response(200, json=PAIR)

    with ExecutorClient("https://executor.invalid", token="fixture-token",
                        transport=httpx.MockTransport(handle)) as client:
        assert client.verified_bindings(PAIR) == {
            "env-aws": "cutover/source", "env-onprem": "cutover/target",
        }
        app = create_operator_cutover_app(None, client, registered_pair=PAIR, verifier=lambda _: None)
        assert any(r.path.endswith("/approvals") for r in app.routes)
        invalid = deepcopy(PAIR)
        invalid["target"]["resource_id"] = "fixture/replaced"
        with pytest.raises(ExecutorReadError, match="registry_mismatch"):
            create_operator_cutover_app(None, client, registered_pair=invalid, verifier=lambda _: None)
    assert calls == [("GET", "/v1/resources")] * 3


@pytest.mark.parametrize("fault", ["swapped", "changed_ref", "extra", "missing", "wrong_type",
                                   "duplicate", "redirect", "denied", "unavailable", "large"])
def test_rejects_changed_registry_and_unsafe_responses(fault):
    payload = deepcopy(PAIR)
    if fault == "swapped":
        payload["source"], payload["target"] = payload["target"], payload["source"]
    elif fault == "changed_ref":
        payload["source"]["connection_ref"] = "other/reader"
    elif fault == "extra":
        payload["target"]["private_fields"] = "PRIVATE_SENTINEL"
    elif fault == "missing":
        del payload["source"]["resource_id"]
    elif fault == "wrong_type":
        payload["target"] = None
    body = json.dumps(payload)
    if fault == "duplicate":
        body = '{"target":null,' + body[1:]
    if fault == "large":
        body += " " * 262144
    status = {"redirect": 302, "denied": 401, "unavailable": 503}.get(fault, 200)
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(status, content=body, headers={
            "Content-Type": "application/json", "Location": "https://elsewhere.invalid",
        })

    with ExecutorClient("https://executor.invalid", token="test", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ExecutorReadError, match=r"^registry_(mismatch|unavailable)$"):
            client.verified_bindings(PAIR)
    assert calls == ["/v1/resources"]


@pytest.mark.parametrize("source,target", [
    ("env-onprem", "env-aws"), ("west-db", "east-db"), ("east-db", "west-db"),
])
def test_registration_defines_direction_independently_of_names_and_key_order(source, target):
    enrolled = {"source": dict(PAIR["source"], environment_id=source),
                "target": dict(PAIR["target"], environment_id=target)}
    remote = {"target": enrolled["target"], "source": enrolled["source"]}
    with ExecutorClient("https://executor.invalid", token="fixture-token",
                        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=remote))) as client:
        assert client.verified_bindings(enrolled) == {source: "cutover/source", target: "cutover/target"}
        with pytest.raises(ExecutorReadError, match="^registry_mismatch$"):
            client.verified_bindings({"source": enrolled["target"], "target": enrolled["source"]})


@pytest.mark.parametrize("invalid_env", [None, "", "postgresql://private.invalid/db", "x" * 129])
def test_invalid_environment_id_stops_before_network(invalid_env):
    invalid = deepcopy(PAIR)
    invalid["source"]["environment_id"] = invalid_env
    with ExecutorClient("https://executor.invalid", token="fixture-token",
                        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected GET"))) as client:
        with pytest.raises(ValueError, match="^invalid_registered_pair$"):
            client.verified_bindings(invalid)


@pytest.mark.parametrize("side", ["source", "target"])
@pytest.mark.parametrize("invalid_env", ["", "postgresql://private.invalid/db"])
def test_remote_environment_id_is_neither_adopted_nor_exposed(side, invalid_env):
    remote = deepcopy(PAIR)
    remote[side]["environment_id"] = invalid_env
    with ExecutorClient("https://executor.invalid", token="fixture-token",
                        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=remote))) as client:
        with pytest.raises(ExecutorReadError, match="^registry_mismatch$"):
            client.verified_bindings(PAIR)


def test_no_credential_or_invalid_enrollment_stops_before_network():
    def handle(request):
        pytest.fail("unexpected request")

    with ExecutorClient("https://executor.invalid", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ExecutorReadError, match="registry_auth_required"):
            client.verified_bindings(PAIR)
        for field in ("environment_id", "connection_ref", "resource_id"):
            bad = deepcopy(PAIR)
            bad["source"][field] = PAIR["target"][field]
            with pytest.raises(ValueError, match="invalid_registered_pair"):
                client.verified_bindings(bad)
        with pytest.raises(ValueError, match="exactly_one_binding_source_required"):
            create_operator_cutover_app(None, client, bindings={}, registered_pair=PAIR, verifier=lambda _: None)
