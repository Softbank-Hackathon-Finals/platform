"""Reader service config v2: the platform Job identity only (the Actions caller is removed)."""

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from bravo_platform_events import product_read_server
from bravo_platform_events.product_api import KubernetesJobCaller

SUBJECT = "system:serviceaccount:bravo-initial:initial-proposal"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def config_files(tmp_path, key):
    public = json.loads(RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "cluster-key"}
    jwks = tmp_path / "jwks.json"
    jwks.write_text(json.dumps({"keys": [public]}))
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    signing = tmp_path / "catalog-key"
    signing.write_bytes(b"c" * 32)
    config = dict(version=2, tenant_id="tenant-fixture", catalog_root=str(catalog), catalog_key_file=str(signing),
                  job_identity=dict(issuer="https://platform-rke2.invalid", audience="bravo-subscription-broker",
                                    subject=SUBJECT, jwks_file=str(jwks)))
    path = tmp_path / "reader.json"
    path.write_text(json.dumps(config))
    return path, config, jwks


def test_v2_config_builds_the_job_identity_reader(backend, tmp_path, key):
    path, _, _ = config_files(tmp_path, key)
    reader = product_read_server.configured_initial_reader(backend.store, path)
    assert type(reader.caller) is KubernetesJobCaller and reader.caller.sender.subject == SUBJECT
    assert reader.caller.sender.tenant_id == "tenant-fixture"
    assert reader.records.authorize.__self__.purpose == "t38-initial-proposal"


@pytest.mark.parametrize("change", [
    {"version": 1},
    {"sts_provider": "fixture", "github_public_jwks_file": "/x"},  # the removed Actions fields
    {"job_identity": None},
])
def test_actions_or_partial_config_refuses_start(tmp_path, key, change):
    path, config, _ = config_files(tmp_path, key)
    path.write_text(json.dumps({k: v for k, v in (config | change).items() if v is not None}))
    with pytest.raises(ValueError):
        product_read_server.configured_initial_reader(object(), path)


def test_private_relative_or_missing_mounts_refuse_start(tmp_path, key):
    path, config, jwks = config_files(tmp_path, key)
    jwks.write_text(json.dumps({"keys": [json.loads(RSAAlgorithm.to_jwk(key)) | {"kid": "cluster-key"}]}))
    with pytest.raises(ValueError, match="job_public_keys_required"):
        product_read_server.configured_initial_reader(object(), path)
    path.write_text(json.dumps(config | {"job_identity": config["job_identity"] | {"jwks_file": "jwks.json"}}))
    with pytest.raises(ValueError, match="invalid_job_configuration"):
        product_read_server.configured_initial_reader(object(), path)
    path.write_text(json.dumps(config | {"catalog_root": str(tmp_path / "missing")}))
    with pytest.raises(ValueError, match="product_mounts_required"):
        product_read_server.configured_initial_reader(object(), path)


def test_old_schema_rejected_before_reader_construction(pg_backend):
    with pg_backend.admin() as c:
        c.execute("UPDATE schema_version SET version=1")
    with pytest.raises(ValueError, match="product_read_schema_required"):
        product_read_server.load_initial_app(pg_backend.store, lambda _: pytest.fail("reader must not be constructed"))


def test_missing_tls_refuses_before_database_and_does_not_print_inputs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(product_read_server, "runtime_connection", lambda: pytest.fail("unexpected credential read"))
    with pytest.raises(SystemExit, match=r"^product_read_startup_unavailable:[A-Za-z]+(:[A-Za-z0-9_.]+)?$"):
        product_read_server.main(["--config", str(tmp_path / "PRIVATE_SENTINEL"), "--initial-proposal",
                                  "--tls-cert", "/missing/cert", "--tls-key", "/missing/key"])
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().out


def test_service_exposes_only_initial_reads(pg_backend, tmp_path, key):
    from bravo_platform_events.initial_proposal_api import PATH as INITIAL_PATH

    path, _, _ = config_files(tmp_path, key)
    with pg_backend.cluster.connect(pg_backend.database, "fixture_migration") as c:
        c.execute((Path(__file__).parents[1] / "sql/0002_sli_windows.sql").read_text())
    with pg_backend.store._transaction() as c:
        before = c.execute("SELECT count(*) AS n FROM plan_commands").fetchone()["n"]
    reader = product_read_server.configured_initial_reader(pg_backend.store, path)
    app = product_read_server.load_initial_app(pg_backend.store, lambda store: reader)
    with TestClient(app, base_url="https://host.invalid") as client:
        assert client.post(INITIAL_PATH, json={}).status_code == 403
        assert client.post("/internal/v1/product-inputs/read", json={}).status_code == 404
        assert client.post("/v1/plans", json={}).status_code == 404
    with pg_backend.store._transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM plan_commands").fetchone()["n"] == before


def test_v1_config_starts_with_the_initial_route_closed_and_v2_still_serves(tmp_path):
    """Rollout order: a new image on the old (v1) config must not crash; the route stays closed."""
    from fastapi.testclient import TestClient

    from bravo_platform_events.initial_proposal_api import PATH

    v1 = {"version": 1, "tenant_id": "t", "sts_provider": "https://sts", "github_public_jwks_file": "/j",
          "catalog_root": "/c", "catalog_key_file": "/k"}
    path = tmp_path / "reader.json"
    path.write_text(json.dumps(v1))
    response = TestClient(product_read_server.configured_app(object(), str(path))).post(PATH, json={})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "initial_route_closed_config_v1"
    path.write_text(json.dumps(v1 | {"extra": 1}))
    with pytest.raises(ValueError):
        product_read_server.configured_app(object(), str(path))
