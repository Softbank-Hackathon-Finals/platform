"""Public deployment registry readback. No connection, migration or worker submission."""

import json

import pytest
from fastapi.testclient import TestClient

from bravo_cutover import server
from bravo_cutover.api import create_app
from bravo_cutover.resources import Registry, Resource
from bravo_cutover.sites import load_config
from tests.test_runner_fakes import site

TOKEN = "registry-projection-test-only-token-of-sufficient-length"
AUTH = {"Authorization": "Bearer " + TOKEN}


def test_deployment_registry_readback_is_authenticated_and_public(tmp_path, monkeypatch):
    # Reversed file order and unrelated IDs catch direction or canned-response mistakes.
    sites = {}
    for side, env, identity in [
        ("target", "env-onprem", "db-b/custom-id"),
        ("source", "env-aws", "db-a/another-id"),
    ]:
        value = site(env, side + "-dev", "subscriber_test").model_dump()
        value.update(
            resource_id=identity,
            connection_ref="cutover/" + side,
            db_control_dsn="postgresql://probe:PRIVATE_MARKER@db.invalid/chat",
        )
        sites[env] = value
    config, token = tmp_path / "sites.json", tmp_path / "token"
    config.write_text(json.dumps({"origin": "env-aws", "sites": sites}))
    token.write_text(TOKEN)
    monkeypatch.setenv("EXECUTOR_CONFIG", str(config))
    monkeypatch.setenv("EXECUTOR_API_TOKEN_FILE", str(token))
    monkeypatch.setenv("EXECUTOR_DB", "postgresql://PRIVATE_STATE_MARKER@state.invalid/chat")
    # Set aside the production factory's unrelated startup DB lock and recovery effects.
    monkeypatch.setattr(server, "Store", lambda _dsn: object())
    monkeypatch.setattr(server, "ThreadRunner", lambda _cutover, _store: object())
    client = TestClient(server.create_app())
    # Bind the response to the registry loaded at startup, even if local files later change.
    config.unlink()
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client.get("/v1/resources", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": {"code": "unauthorized"}}
    result = client.get("/v1/resources", headers=AUTH)
    assert result.status_code == 200
    assert result.json() == {
        "source": {
            "environment_id": "env-aws",
            "resource_id": "db-a/another-id",
            "connection_ref": "cutover/source",
        },
        "target": {
            "environment_id": "env-onprem",
            "resource_id": "db-b/custom-id",
            "connection_ref": "cutover/target",
        },
    }
    spec = client.get("/openapi.json").json()
    schema = spec["paths"]["/v1/resources"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    components = spec["components"]["schemas"]
    pair = components[schema["$ref"].split("/")[-1]]["properties"]
    assert set(pair) == {"source", "target"}
    for field in pair.values():
        assert set(components[field["$ref"].split("/")[-1]]["properties"]) == {
            "environment_id",
            "resource_id",
            "connection_ref",
        }
    for marker in [
        "PRIVATE_MARKER",
        "PRIVATE_STATE_MARKER",
        "db_control_dsn",
        "pgbouncer_admin_dsn",
        TOKEN,
    ]:
        assert marker not in result.text and marker not in json.dumps(spec)


@pytest.mark.parametrize(
    "extra,invalid_ref",
    [
        (True, None),
        (False, "postgresql://app:password@db.invalid/chat"),
        (False, "host=db.invalid password=private"),
    ],
)
def test_ambiguous_registry_or_dsn_is_not_exposed(extra, invalid_ref):
    resources = [
        Resource(environment_id=env, resource_id=env + "/chat", connection_ref=ref)
        for env, ref in [
            ("env-aws", invalid_ref or "cutover/source"),
            ("env-onprem", "cutover/target"),
        ]
    ]
    if extra:
        resources.append(
            Resource(environment_id="third", resource_id="another", connection_ref="some/ref")
        )
    client = TestClient(
        create_app(store=object(), runner=object(), registry=Registry(resources, origin="env-aws"),
                   api_token=TOKEN)
    )
    response = client.get("/v1/resources", headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "executor_not_configured"}}


@pytest.mark.parametrize("token,target_id", [(None, "db-b"), (TOKEN, "db-a")])
def test_authentication_and_distinct_databases_are_required(token, target_id):
    registry = Registry([
        Resource(environment_id="env-aws", resource_id="db-a", connection_ref="cutover/source"),
        Resource(environment_id="env-onprem", resource_id=target_id, connection_ref="cutover/target"),
    ], origin="env-aws")
    client = TestClient(create_app(store=object(), registry=registry, api_token=token))
    response = client.get("/v1/resources", headers=AUTH if token else {})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "executor_not_configured"}}


# T20: 등록 조회의 source와 영수증 방향은 같은 원래 출발지(origin)를 쓴다 ----------------------------

def start_server(tmp_path, monkeypatch, origin, order):
    """실제 배포 기동 함수(server.create_app)로 설정 파일을 읽힌다. order는 sites의 JSON 순서다."""
    sites = {}
    for env, identity, ref in order:
        value = site(env, env + "-ns", "subscriber_test").model_dump()
        value.update(resource_id=identity, connection_ref=ref)
        sites[env] = value
    config, token = tmp_path / "sites.json", tmp_path / "token"
    config.write_text(json.dumps({"origin": origin, "sites": sites}))
    token.write_text(TOKEN)
    monkeypatch.setenv("EXECUTOR_CONFIG", str(config))
    monkeypatch.setenv("EXECUTOR_API_TOKEN_FILE", str(token))
    monkeypatch.setenv("EXECUTOR_DB", "postgresql://state.invalid/chat")
    monkeypatch.setattr(server, "Store", lambda _dsn: object())
    monkeypatch.setattr(server, "ThreadRunner", lambda _cutover, _store: object())
    return TestClient(server.create_app()), load_config(config)


PAIRS = {
    "aws-onprem": [("env-aws", "db-a/aws", "cutover/source"), ("env-onprem", "db-b/onprem", "cutover/target")],
    # 클라우드 간 이전: 환경 이름을 고정하지 않는다
    "gcp-aws": [("env-gcp", "db-g/gcp", "cutover/gcp"), ("env-aws", "db-a/aws", "cutover/aws")],
}


@pytest.mark.parametrize("pair", PAIRS)
@pytest.mark.parametrize("origin_index", [0, 1])
@pytest.mark.parametrize("reverse", [False, True])
def test_registry_source_follows_origin_and_matches_receipt_direction(tmp_path, monkeypatch, pair, origin_index,
                                                                       reverse):
    order = PAIRS[pair]
    origin = order[origin_index][0]
    client, config = start_server(tmp_path, monkeypatch, origin, list(reversed(order)) if reverse else order)
    result = client.get("/v1/resources", headers=AUTH)
    assert result.status_code == 200
    projected = result.json()
    assert projected["source"]["environment_id"] == origin
    assert projected["target"]["environment_id"] != origin
    # 같은 서버가 이 쌍으로 실행하면 영수증은 cutover, 반대 방향이면 failback이다
    source, target = projected["source"]["environment_id"], projected["target"]["environment_id"]
    assert config.direction(source, target) == "cutover"
    assert config.direction(target, source) == "failback"


def test_registry_without_origin_is_not_exposed():
    """자원 파일만 읽는 기록 전용 서버는 원래 출발지를 모르므로 등록 쌍을 공개하지 않는다."""
    registry = Registry([
        Resource(environment_id="env-aws", resource_id="db-a", connection_ref="cutover/source"),
        Resource(environment_id="env-onprem", resource_id="db-b", connection_ref="cutover/target"),
    ])
    client = TestClient(create_app(store=object(), registry=registry, api_token=TOKEN))
    response = client.get("/v1/resources", headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "executor_not_configured"}}


@pytest.mark.parametrize("invalid", ["", "postgresql://invalid.example/chat", "a" * 129])
@pytest.mark.parametrize("side", [0, 1])
def test_invalid_environment_is_not_published(tmp_path, monkeypatch, invalid, side):
    order = [("env-one", "db-a", "cutover/source"), ("env-two", "db-b", "cutover/target")]
    order[side] = (invalid, *order[side][1:])
    client, _ = start_server(tmp_path, monkeypatch, order[0][0], order)
    response = client.get("/v1/resources", headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "executor_not_configured"}}
