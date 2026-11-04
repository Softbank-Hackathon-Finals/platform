"""Registration API and transaction behavior on SQLite and temporary PostgreSQL."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.registration import Registry
from bravo_platform_events.registration_api import create_registration_app
from bravo_platform_events.registration_probe import EnvironmentRef, ProbeUnavailable, SourceRelease

SQL = Path(__file__).parents[1] / "sql"
TENANT = "team"
SRC = dict(name="source", kind="aws", roles=["source"], connection_ref="source-reader")
DST = dict(name="target", kind="onprem", roles=["target"], connection_ref="target-reader")
APP = dict(name="chat", repo_url="https://example.invalid/chat", app_path="app", iac_path="compose.yaml",
           source_environment_id="env-src", namespace="app-src")


class Probe:
    def __init__(self):
        self.results = []
        self.fault = None
        self.after = lambda: None

    def inspect(self, env, release=None):
        self.results.append((env.environment_id, release is not None))
        if self.fault == "denied":
            raise ProbeUnavailable("required_read_denied")
        result = dict(namespace=env.namespace, database="chat", database_identity=env.database_uid,
                      collector="scoped-reader", warnings=[], read_access="verified")
        if self.fault == "replacement":
            result["database_identity"] = "recreated"
        if release:
            result.update(rollout_identity="fixture-rs", source_sha="a" * 40, image="b" * 64, service_account="chat", repo_deploy_match=True,
                          resources=[{"kind": "Cluster", "name": "chat"}])
            if self.fault == "drift":
                result["source_sha"] = "e" * 40
        self.after()
        return result


class Fixture:
    def __init__(self, backend, temp):
        self.backend = backend
        if backend.cluster:
            with backend.cluster.connect(backend.database, "fixture_migration") as c:
                for script in ("0002_sli_windows.sql", "0003_registrations.sql"):
                    c.execute((SQL / script).read_text())
        self.store = backend.reopen()
        self.now = datetime.now(UTC)
        epoch = int(self.now.timestamp())
        self.actor = OperatorPrincipal("operator", TENANT, epoch - 1, epoch + 300, epoch - 1, "test-session")
        self.allowed = True
        self.probe = Probe()
        src = EnvironmentRef(TENANT, "env-src", "aws", "app-src", "db", "src-id",
                             "https://source.invalid", temp / "ca", lambda: "test-only")
        dst = replace(src, environment_id="env-dst", kind="onprem", namespace="app-dst", database_uid="dst-id")
        release = SourceRelease(TENANT, "env-src", APP["repo_url"], temp, "app", "compose.yaml", "a" * 40,
                                "chat", "chat", "repo/chat@sha256:" + "b" * 64, ("sha256:" + "b" * 64,), "chat", "rs-uid")
        self.registry = Registry(self.store, environments={(TENANT, "source-reader"): src, (TENANT, "target-reader"): dst},
                                 releases=[release], can_register=lambda _a: self.allowed, probe=self.probe)
        self.client = TestClient(create_registration_app(self.registry, verify_identity=lambda _r: self.actor))

    def post(self, route, data, key):
        return self.client.post("/v1/" + route, json=data, headers={"Idempotency-Key": key})

    def setup_workload(self, source=SRC):
        assert self.post("environments", source, "src-key").status_code == 201
        assert self.post("environments", DST, "dst-key").status_code == 201
        app = self.post("workloads", APP, "app-key")
        assert app.status_code == 201
        return {"workload_id": app.json()["workload_id"], "target_environment_id": "env-dst"}


@pytest.fixture
def registered(backend, tmp_path):
    f = Fixture(backend, tmp_path)
    yield f
    f.client.close()


def test_create_plan_and_retry_returns_committed_snapshot(registered):
    f = registered
    body = f.setup_workload()
    created = f.post("plans", body, "plan-key")
    assert created.status_code == 201
    receipt = created.json()
    assert f.probe.results[-2:] == [("env-src", True), ("env-dst", False)]
    snap = f.backend.reopen().snapshot(receipt["plan_id"])
    assert snap["state"] == "COLLECT" and snap["version"] == 1
    assert snap["plan_digest"] == receipt["plan_digest"] and snap["collection_scope_digest"] == receipt["collection_scope_digest"]
    f.probe.fault = "denied"
    retry = f.post("plans", body, "plan-key")
    assert retry.status_code == 200 and retry.json() == receipt
    assert f.post("plans", body | {"target_environment_id": "env-other"}, "plan-key").status_code == 409
    assert len(f.store.read_after(receipt["plan_id"], None)) == 1


def test_missing_foreign_or_unapproved_refs_never_probe(registered):
    f = registered
    assert f.post("environments", SRC | {"connection_ref": "https://attacker.invalid"}, "bad").status_code == 422
    assert f.post("environments", SRC | {"endpoint": "https://attacker.invalid"}, "bad").status_code == 422
    assert f.post("environments", SRC | {"connection_ref": "unknown"}, "bad").status_code == 404
    assert f.post("environments", SRC | {"roles": ["target"]}, "bad").status_code == 409
    assert f.probe.results == []
    f.setup_workload()
    f.probe.results.clear()
    f.actor = replace(f.actor, tenant_id="other")
    assert f.post("workloads", APP, "app-key").status_code == 404
    assert f.post("environments", SRC, "src-key").status_code == 404
    assert f.probe.results == []


def test_onprem_source_requires_its_own_catalog_role(registered):
    f = registered
    ref = (TENANT, SRC["connection_ref"])
    f.registry.environments[ref] = replace(f.registry.environments[ref], kind="onprem", catalog_role="source")
    source = SRC | {"kind": "onprem"}
    assert f.post("environments", source | {"connection_ref": DST["connection_ref"]}, "bad-src").status_code == 409
    assert f.post("environments", source | {"roles": ["target"]}, "bad-dst").status_code == 409
    assert f.probe.results == []
    body = f.setup_workload(source)
    created = f.post("plans", body, "plan-key")
    assert created.status_code == 201
    receipt = created.json()
    assert (receipt["source_environment_id"], receipt["target_environment_id"], receipt["state"]) == (
        "env-src", "env-dst", "COLLECT")
    assert f.post("plans", body, "plan-key").json() == receipt


@pytest.mark.parametrize(("kind", "role"), [("aws", "target"), ("onprem", "both"), ("onprem", "")])
def test_invalid_catalog_role_is_rejected(tmp_path, kind, role):
    with pytest.raises(ValueError, match="invalid_environment_role"):
        EnvironmentRef(TENANT, "env", kind, "app", "db", "uid", "https://api.example.invalid",
                       tmp_path / "ca", lambda: "test-only", catalog_role=role)


def test_existing_source_receipt_cannot_be_rebound_to_another_kind(registered):
    f = registered
    assert f.post("environments", SRC, "src-key").status_code == 201
    ref = (TENANT, SRC["connection_ref"])
    f.registry.environments[ref] = replace(f.registry.environments[ref], kind="onprem", catalog_role="source")
    assert f.post("environments", SRC, "src-key").status_code == 200
    f.probe.results.clear()
    refused = f.post("workloads", APP, "app-key")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "environment_changed"
    assert f.probe.results == []


@pytest.mark.parametrize("fault", ["replacement", "denied", "drift"])
def test_plan_rechecks_current_reference_before_any_plan_is_created(registered, fault):
    f = registered
    body = f.setup_workload()
    f.probe.fault = fault
    response = f.post("plans", body, "plan-key")
    assert response.status_code == (503 if fault == "denied" else 409)
    with f.backend.admin() as c:
        assert c.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM registrations WHERE kind='plan'").fetchone()[0] == 0


def test_grant_revoked_during_probe_is_denied_before_commit(registered):
    f = registered
    f.probe.after = lambda: setattr(f, "allowed", False)
    assert f.post("environments", SRC, "key").status_code == 403
    f.allowed = True
    f.probe.after = lambda: None
    assert f.post("environments", SRC, "key").status_code == 201


def test_no_idp_and_expired_operator_fail_before_probes_or_replay(registered):
    f = registered
    actor = f.actor
    f.actor = None
    assert f.post("environments", SRC, "src").status_code == 403
    f.actor = replace(actor, expires_at=0)
    assert f.post("environments", SRC, "src").status_code == 403
    assert f.probe.results == []


def test_source_role_and_repo_scope_cannot_be_substituted(registered):
    f = registered
    body = f.setup_workload()
    assert f.post("workloads", APP | {"namespace": "other"}, "bad").status_code == 409
    assert f.post("workloads", APP | {"repo_url": "https://attacker.invalid"}, "bad").status_code == 404
    assert f.post("workloads", APP | {"app_path": "../"}, "bad").status_code == 404
    assert f.post("plans", body | {"target_environment_id": "env-src"}, "bad").status_code == 409
    assert f.post("environments", SRC, "another-key").status_code == 409


def test_event_write_failure_rolls_back_plan_and_key(registered):
    f = registered
    body = f.setup_workload()
    with f.backend.reject_insert("events"):
        assert f.post("plans", body, "key").status_code == 503
    with f.backend.admin() as c:
        for table in ("plans", "events", "streams"):
            assert c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0
    assert f.post("plans", body, "key").status_code == 201


def test_same_key_in_parallel_inserts_one_plan_and_event(registered):
    f = registered
    body = f.setup_workload()
    barrier = Barrier(4)
    def submit(_):
        barrier.wait(timeout=10)
        return f.post("plans", body, "key")
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(submit, range(4)))
    assert sorted(r.status_code for r in replies) == [200, 200, 200, 201]
    assert len({json.dumps(r.json(), sort_keys=True) for r in replies}) == 1


def test_registration_migration_grants_runtime_only_read_and_insert(pg_backend, tmp_path):
    f = Fixture(pg_backend, tmp_path)
    try:
        with pg_backend.cluster.connect(pg_backend.database, "fixture_runtime") as c:
            rights = c.execute("SELECT has_table_privilege(current_user,'bravo_platform.registrations',p) "
                               "FROM unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE']) p").fetchall()
            assert [v[0] for v in rights] == [True, True, False, False, False]
    finally:
        f.client.close()


def test_operator_entrypoint_verifies_signed_identity_and_current_grant(tmp_path, monkeypatch):
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from bravo_platform_events.operator_identity import operator_id
    from bravo_platform_events.plans import PlanStore
    from bravo_platform_events.registration_probe import KubernetesProbe
    from bravo_platform_events.registration_server import build_app

    issuer = "https://id.example.invalid"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="test-key", alg="RS256", use="sig")
    config = dict(operator_verifier=dict(issuer=issuer, audience="api", client_id="ui", assurance="interactive",
                  subjects={"owner": TENANT}, public_jwks={"keys": [public]}),
                  environments=[dict(tenant_id=TENANT, environment_id="src", connection_ref="source-reader",
                        kind="aws", namespace="src", database="db", database_uid="database",
                        endpoint="https://api.example.invalid", ca_file=str(tmp_path / "ca"),
                        token_file=str(tmp_path / "token"))], releases=[])
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(config))
    grants = tmp_path / "grants.json"
    grants.write_text(json.dumps([{"tenant_id": TENANT, "subject_id": operator_id(issuer, "owner")}]))
    now = int(time.time())
    token = jwt.encode(dict(iss=issuer, sub="owner", aud="api", iat=now, nbf=now, exp=now+60,
                            auth_time=now, jti="id", client_id="ui", acr="interactive"),
                       key, algorithm="RS256", headers={"kid": "test-key", "typ": "at+jwt"})
    monkeypatch.setattr(KubernetesProbe, "inspect", lambda _, env: Probe().inspect(env))
    store = PlanStore(tmp_path / "entry.db")
    with TestClient(build_app(store, catalog, grants)) as client:
        url = "/v1/environments"
        headers = {"Idempotency-Key": "key", "Authorization": "Bearer " + token}
        assert client.post(url, json=SRC, headers={"Idempotency-Key": "key", "X-Tenant-ID": TENANT}).status_code == 403
        assert client.post(url, json=SRC, headers=headers).status_code == 201
        assert client.post(url, json=SRC, headers=list(headers.items()) + [("Idempotency-Key", "second")]).status_code == 422
        grants.write_text("[]")
        assert client.post(url, json=SRC, headers=headers).status_code == 403
