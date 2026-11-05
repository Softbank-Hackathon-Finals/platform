"""Private configuration and schema-2 service reads need no human IdP or DDL."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from test_product_api import key as key
from test_product_inputs import Fixture

from bravo_platform_events import product_read_server
from bravo_platform_events.product_api import PATH


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
