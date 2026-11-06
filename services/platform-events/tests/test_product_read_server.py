"""Private configuration and schema-2 service reads need no human IdP or DDL."""

import json
import socket
from dataclasses import replace
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from test_product_api import api as api
from test_product_api import key as key
from test_product_api import token
from test_product_inputs import Fixture

from bravo_platform_events import product_read_server
from bravo_platform_events.product_api import PATH, create_product_read_app
from bravo_platform_events.store import EventError


def config_files(tmp_path, key):
    public = json.loads(RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "fixture-key"}
    jwks = tmp_path / "public.json"
    jwks.write_text(json.dumps({"keys": [public]}))
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    signing = tmp_path / "catalog-key"
    signing.write_bytes(b"c" * 32)
    config = dict(version=1, tenant_id="tenant-fixture", sts_provider="fixture-observed-provider",
        github_public_jwks_file=str(jwks), github_key_id="fixture-key",
        catalog_root=str(catalog), catalog_key_file=str(signing))
    path = tmp_path / "reader.json"
    path.write_text(json.dumps(config))
    return path, config, jwks


def test_configured_service_accepts_schema2_without_operator_identity_or_writes(pg_backend, tmp_path, key):
    f = Fixture(pg_backend)
    config, _, _ = config_files(tmp_path, key)
    with pg_backend.cluster.connect(pg_backend.database, "fixture_migration") as c:
        c.execute((Path(__file__).parents[1] / "sql/0002_sli_windows.sql").read_text())
    with pg_backend.store._transaction() as c:
        before = c.execute("SELECT count(*) AS n FROM plan_commands").fetchone()["n"]
    app = product_read_server.load_app(pg_backend.store,
        lambda store: product_read_server.configured_reader(store, config))
    with TestClient(app, base_url="https://host.invalid") as client:
        assert client.post(PATH, json={}).status_code == 403
        assert client.post("/v1/plans", json={}).status_code == 404
    with pg_backend.store._transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM plan_commands").fetchone()["n"] == before
        assert c.execute("SELECT version FROM schema_version").fetchone()["version"] == 2
    assert f.calls == [f.admission]


def test_old_schema_rejected_before_reader_construction(pg_backend):
    with pg_backend.admin() as c:
        c.execute("UPDATE schema_version SET version=1")
    with pytest.raises(ValueError, match="product_read_schema_required"):
        product_read_server.load_app(pg_backend.store, lambda _: pytest.fail("reader must not be constructed"))


def test_private_or_ambiguous_public_keys_and_missing_mounts_refuse_start(tmp_path, key):
    path, config, jwks = config_files(tmp_path, key)
    private = json.loads(RSAAlgorithm.to_jwk(key)) | {"kid": "fixture-key"}
    jwks.write_text(json.dumps({"keys": [private]}))
    with pytest.raises(ValueError, match="product_public_keys_required"):
        product_read_server.configured_reader(object(), path)
    public = json.loads(RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "fixture-key"}
    jwks.write_text(json.dumps({"keys": [public, public]}))
    with pytest.raises(ValueError, match="product_public_key_required"):
        product_read_server.configured_reader(object(), path)
    path.write_text(json.dumps(config | {"catalog_root": str(tmp_path / "missing")}))
    with pytest.raises(ValueError, match="product_mounts_required"):
        product_read_server.configured_reader(object(), path)


def test_missing_tls_refuses_before_database_and_does_not_print_inputs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(product_read_server, "runtime_connection", lambda: pytest.fail("unexpected credential read"))
    with pytest.raises(SystemExit, match="^product_read_startup_unavailable$"):
        product_read_server.main(["--config", str(tmp_path / "PRIVATE_SENTINEL"),
                                  "--tls-cert", "/missing/cert", "--tls-key", "/missing/key"])
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().out


def keyset_config(tmp_path, key):
    path, config, jwks = config_files(tmp_path, key)
    config.pop("github_key_id")
    path.write_text(json.dumps(config))
    return path, config, jwks


def test_current_read_accepts_each_local_key_and_still_checks_current_grant(api, tmp_path, key, monkeypatch):
    _, caller, access, grant, reader, ref = api
    path, config, jwks = keyset_config(tmp_path, key)
    path.write_text(json.dumps(config | {"tenant_id": caller.sender.tenant_id}))
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    keys = [("fixture-one", key), ("fixture-two", other)]
    jwks.write_text(json.dumps({"keys": [json.loads(RSAAlgorithm.to_jwk(k.public_key())) | {"kid": kid}
                                       for kid, k in keys]}))
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: pytest.fail("JWT must not fetch keys"))
    selected = product_read_server.configured_reader(reader.records.store, path).caller
    app = create_product_read_app(replace(reader, caller=selected))
    with TestClient(app, base_url="https://host.invalid") as client:
        for kid, signing in keys:
            claims = jwt.decode(token(signing), options={"verify_signature": False})
            credential = jwt.encode(claims, signing, algorithm="RS256", headers={"kid": kid})
            response = client.post(PATH, json={"principal": caller.principal, "input_ref": ref["input_ref"]},
                                   headers={"Authorization": "Bearer " + credential})
            assert response.status_code == 200
            assert response.json()["input_digest"] == ref["input_digest"]
        access.set_grant(reader.records.read(caller.principal, ref["input_ref"])["admission"]["plan_id"],
            caller.principal, **(grant | dict(enabled=False, expected_revision=1, command_key="revoke")))
        assert client.post(PATH, json={"principal": caller.principal, "input_ref": ref["input_ref"]},
                           headers={"Authorization": "Bearer " + credential}).status_code == 409


@pytest.mark.parametrize("headers", [{}, {"kid": "unknown"}, {"kid": []},
    {"kid": "fixture-key", "jku": "https://attacker.invalid/keys"},
    {"kid": "fixture-key", "jwk": {}}, {"kid": "fixture-key", "x5u": "https://attacker.invalid/cert"}])
def test_keyset_rejects_missing_unknown_or_embedded_key_headers(backend, tmp_path, key, headers):
    path, _, _ = keyset_config(tmp_path, key)
    caller = product_read_server.configured_reader(backend.store, path).caller
    claims = jwt.decode(token(key), options={"verify_signature": False})
    # Construct raw malformed headers as PyJWT correctly refuses a non-string kid at encode time.
    if isinstance(headers.get("kid"), list):
        import base64
        encoded = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"} | headers).encode()).rstrip(b"=").decode()
        credential = encoded + "." + token(key).split(".", 1)[1]
    else:
        credential = jwt.encode(claims, key, algorithm="RS256", headers=headers)
    with pytest.raises(EventError, match="forbidden"):
        caller.authenticate(credential, caller.principal)


@pytest.mark.parametrize("change", [{"iss": "https://other.invalid"}, {"aud": "other"}, {"sub": "other"},
    {"repository_id": "1"}, {"repository_owner_id": "1"}, {"exp": 1}])
def test_key_selection_does_not_relax_identity(backend, tmp_path, key, change):
    path, _, _ = keyset_config(tmp_path, key)
    caller = product_read_server.configured_reader(backend.store, path).caller
    claims = jwt.decode(token(key), options={"verify_signature": False}) | change
    credential = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture-key"})
    with pytest.raises(EventError, match="forbidden"):
        caller.authenticate(credential, caller.principal)


def test_keyset_rejects_forged_signature_and_algorithm(backend, tmp_path, key):
    path, _, _ = keyset_config(tmp_path, key)
    caller = product_read_server.configured_reader(backend.store, path).caller
    claims = jwt.decode(token(key), options={"verify_signature": False})
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for signing, algorithm in ((other, "RS256"), (b"synthetic-fixture-hmac-key" * 2, "HS256")):
        credential = jwt.encode(claims, signing, algorithm=algorithm, headers={"kid": "fixture-key"})
        with pytest.raises(EventError, match="forbidden"):
            caller.authenticate(credential, caller.principal)


@pytest.mark.parametrize("change", [{"kid": ""}, {"kid": None}, {"kid": "k" * 257}, {"kty": "EC"},
    {"alg": "HS256"}, {"use": "enc"}, {"key_ops": ["sign"]}, {"d": "private-fixture"}])
def test_every_local_key_is_validated_at_startup(tmp_path, key, change):
    path, _, jwks = keyset_config(tmp_path, key)
    public = json.loads(RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "fixture-key"}
    jwks.write_text(json.dumps({"keys": [public, public | change]}))
    with pytest.raises(ValueError):
        product_read_server.configured_reader(object(), path)


def test_duplicate_key_id_refuses_keyset_startup(tmp_path, key):
    path, _, jwks = keyset_config(tmp_path, key)
    public = json.loads(RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "fixture-key"}
    jwks.write_text(json.dumps({"keys": [public, public]}))
    with pytest.raises(ValueError, match="invalid_workload_keys"):
        product_read_server.configured_reader(object(), path)


def test_initial_service_exposes_only_claimed_initial_reads_and_uses_separate_grant(pg_backend, tmp_path, key):
    from bravo_platform_events.initial_proposal_api import PATH as INITIAL_PATH

    path, _, _ = config_files(tmp_path, key)
    with pg_backend.cluster.connect(pg_backend.database, "fixture_migration") as c:
        c.execute((Path(__file__).parents[1] / "sql/0002_sli_windows.sql").read_text())
    with pg_backend.store._transaction() as c:
        before = c.execute("SELECT count(*) AS n FROM plan_commands").fetchone()["n"]
    reader = product_read_server.configured_initial_reader(pg_backend.store, path)
    assert reader.records.authorize.__self__.purpose == "t38-initial-proposal"
    app = product_read_server.load_initial_app(pg_backend.store, lambda store: reader)
    with TestClient(app, base_url="https://host.invalid") as client:
        assert client.post(INITIAL_PATH, json={}).status_code == 403
        assert client.post(PATH, json={}).status_code == 404
        assert client.post("/v1/plans", json={}).status_code == 404
    with pg_backend.store._transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM plan_commands").fetchone()["n"] == before
