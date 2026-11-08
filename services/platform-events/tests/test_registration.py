"""Registration API and transaction behavior on SQLite and temporary PostgreSQL."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from bravo_platform_events import registration_kinds
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
PROFILE = dict(profile_id="full", profile_version="1")
RECHECK = dict(recheck_profile_id="apply-recheck", recheck_profile_version="1")


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
        if env.cluster_uid is not None:
            result["cluster_identity"] = "cluster:" + env.cluster_uid
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
                                 releases=[release], can_register=lambda _a: self.allowed, probe=self.probe,
                                 evaluation_profiles=[dict(tenant_id=TENANT, **PROFILE)])
        self.client = TestClient(create_registration_app(self.registry, verify_identity=lambda _r: self.actor))

    def post(self, route, data, key):
        return self.client.post("/v1/" + route, json=data, headers={"Idempotency-Key": key})

    def setup_workload(self, source=SRC):
        assert self.post("environments", source, "src-key").status_code == 201
        assert self.post("environments", DST, "dst-key").status_code == 201
        app = self.post("workloads", APP, "app-key")
        assert app.status_code == 201
        return {"workload_id": app.json()["workload_id"], "target_environment_id": "env-dst", **PROFILE}


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
    assert {k: snap[k] for k in PROFILE} == {k: receipt[k] for k in PROFILE} == PROFILE
    assert receipt["workload_id"].startswith("workload-") and receipt["workload_id"] != APP["name"]
    f.probe.fault = "denied"
    retry = f.post("plans", body, "plan-key")
    assert retry.status_code == 200 and retry.json() == receipt
    assert f.post("plans", body | {"target_environment_id": "env-other"}, "plan-key").status_code == 409
    assert len(f.store.read_after(receipt["plan_id"], None)) == 1


@pytest.mark.parametrize("change", [
    {"profile_id": "demo-gate"}, {"profile_version": "2"},
])
def test_unapproved_profile_fails_before_probe_or_any_write(registered, change):
    f = registered
    body = f.setup_workload()
    f.probe.results.clear()
    response = f.post("plans", body | change, "bad-profile")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "evaluation_profile_unavailable"
    assert f.probe.results == []
    with f.backend.admin() as c:
        assert c.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM registrations WHERE kind='plan'").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["profile_id", "profile_version"])
@pytest.mark.parametrize("value", [None, "", 1, "invalid value", "x" * 129])
def test_profile_fields_are_required_bounded_strings(registered, field, value):
    f = registered
    body = f.setup_workload()
    body[field] = value
    f.probe.results.clear()
    assert f.post("plans", body, "bad-profile").status_code == 422
    body.pop(field)
    assert f.post("plans", body, "missing-profile").status_code == 422
    assert f.probe.results == []


def test_profile_is_part_of_plan_digest_and_cannot_change_on_replay(registered):
    from bravo_platform_events.registration import digest

    f = registered
    body = f.setup_workload()
    receipt = f.post("plans", body, "key").json()
    expected = dict(plan_id=receipt["plan_id"], tenant_id=TENANT, workload_id=body["workload_id"],
                    collection_scope_digest=receipt["collection_scope_digest"],
                    target_environment_id="env-dst", target_database_identity="dst-id", **PROFILE)
    assert receipt["plan_digest"] == digest(expected)
    assert digest(expected | {"profile_version": "2"}) != receipt["plan_digest"]
    assert f.post("plans", body | {"profile_version": "2"}, "key").status_code == 409
    assert f.post("plans", body, "key").json() == receipt
    assert f.backend.reopen().snapshot(receipt["plan_id"])["profile_version"] == "1"


def test_profile_policy_is_tenant_bound_and_missing_policy_denies_new_plans(registered):
    f = registered
    body = f.setup_workload()
    for policy in (frozenset(), frozenset({("other-tenant", "full", "1")})):
        f.registry.evaluation_profiles = policy
        f.probe.results.clear()
        assert f.post("plans", body, "key").json()["detail"]["code"] == "evaluation_profile_unavailable"
        assert f.probe.results == []


def test_recheck_profile_is_immutable_tenant_allowed_and_digest_bound(registered):
    from bravo_platform_events.registration import digest

    f = registered
    body = f.setup_workload() | RECHECK
    f.probe.results.clear()
    for other in ((), (("other-tenant", "apply-recheck", "1"),)):
        f.registry.evaluation_profiles = frozenset(((TENANT, "full", "1"), *other))
        assert f.post("plans", body, "recheck").json()["detail"]["code"] == "evaluation_profile_unavailable"
        assert not f.probe.results
    f.registry.evaluation_profiles |= {(TENANT, "apply-recheck", "1")}
    result = f.post("plans", body, "recheck")
    assert result.status_code == 201
    receipt = result.json()
    expected = dict(plan_id=receipt["plan_id"], tenant_id=TENANT, workload_id=body["workload_id"],
                    collection_scope_digest=receipt["collection_scope_digest"],
                    target_environment_id="env-dst", target_database_identity="dst-id", **PROFILE, **RECHECK)
    assert receipt["plan_digest"] == digest(expected)
    for field in RECHECK:
        assert digest(expected | {field: "other"}) != receipt["plan_digest"]
        assert f.post("plans", body | {field: "other"}, "recheck").status_code == 409
        assert f.backend.reopen().snapshot(receipt["plan_id"])[field] == RECHECK[field]
    f.registry.evaluation_profiles = frozenset()
    f.probe.results.clear()
    replay = f.post("plans", body, "recheck")
    assert replay.status_code == 200 and replay.json() == receipt
    assert not f.probe.results


@pytest.mark.parametrize("fields", [
    {"recheck_profile_id": "apply-recheck"}, {"recheck_profile_version": "1"},
    {"recheck_profile_id": None, "recheck_profile_version": None},
    {"recheck_profile_id": "apply-recheck", "recheck_profile_version": 1},
    {"recheck_profile_id": "invalid value", "recheck_profile_version": "1"},
])
def test_recheck_pair_rejected_before_probe(registered, fields):
    f = registered
    body = f.setup_workload()
    f.probe.results.clear()
    assert f.post("plans", body | fields, "invalid-recheck").status_code == 422
    assert not f.probe.results


def test_old_profile_plan_replays_without_nulls_or_in_place_recheck_upgrade(registered):
    f = registered
    body = f.setup_workload()
    receipt = f.post("plans", body, "legacy-profile").json()
    with f.store._transaction() as c:
        original = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (receipt["plan_id"],)).fetchone())
    assert not set(RECHECK) & receipt.keys()
    assert f.post("plans", body, "legacy-profile").json() == receipt
    assert f.post("plans", body | RECHECK, "legacy-profile").status_code == 409
    with f.store._transaction() as c:
        assert dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (receipt["plan_id"],)).fetchone()) == original


def test_old_plan_replay_is_preserved_but_cannot_add_profile_in_place(registered):
    from bravo_platform_events.plans import PlanSpec, _canonical
    from bravo_platform_events.registration import digest

    f = registered
    body = f.setup_workload()
    old_body = {k: v for k, v in body.items() if k not in PROFILE}
    plan_id = "plan-" + digest([TENANT, "plan", "old-key"])[:32]
    spec = PlanSpec(plan_id, TENANT, body["workload_id"], "env-src", "env-dst", "a" * 64, "b" * 64)
    f.store.register_plan(spec, run_id=plan_id, occurred_at=f.now.isoformat())
    old_receipt = dict(plan_id=plan_id, plan_digest=spec.plan_digest, state="COLLECT", version=1)
    # Restore a pre-profile registration as an immutable historical fixture.
    with f.store._transaction(write=True, plan_id=plan_id) as c:
        c.execute("INSERT INTO registrations VALUES (?,?,?,?,?,?)",
                  (TENANT, "plan", plan_id, "old-key",
                   _canonical(dict(actor=f.actor.subject_id, body=old_body)), _canonical(old_receipt)))
    assert f.registry.register(f.actor, "plan", old_body, "old-key") == (old_receipt, False)
    assert f.post("plans", body, "old-key").status_code == 409
    assert "profile_id" not in f.backend.reopen().snapshot(plan_id)
    fresh = f.post("plans", body, "new-key")
    assert fresh.status_code == 201 and fresh.json()["plan_id"] != plan_id
    assert f.backend.reopen().snapshot(plan_id)["plan_digest"] == spec.plan_digest


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


def same_kind(f, kind, *, source_cluster="cluster-a", target_cluster="cluster-b"):
    """Re-provision both references as one kind; same-kind plans need cluster identity."""
    for ref, role, cluster in ((SRC["connection_ref"], "source", source_cluster),
                               (DST["connection_ref"], "target", target_cluster)):
        key = (TENANT, ref)
        f.registry.environments[key] = replace(f.registry.environments[key], kind=kind,
                                               catalog_role=role, cluster_uid=cluster)
    return SRC | {"kind": kind}, DST | {"kind": kind}


def register_pair(f, source, target):
    assert f.post("environments", source, "src-key").status_code == 201
    assert f.post("environments", target, "dst-key").status_code == 201
    app = f.post("workloads", APP, "app-key")
    assert app.status_code == 201
    return {"workload_id": app.json()["workload_id"], "target_environment_id": "env-dst", **PROFILE}


def all_slots_ready(f, *kinds):
    f.registry.adapter_readiness = registration_kinds.adapter_readiness(
        {kind: list(registration_kinds.SLOTS) for kind in kinds})


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


@pytest.mark.parametrize(("kind", "role"), [("onprem", "both"), ("onprem", ""), ("gcp", "primary")])
def test_invalid_catalog_role_is_rejected(tmp_path, kind, role):
    with pytest.raises(ValueError, match="invalid_environment_role"):
        EnvironmentRef(TENANT, "env", kind, "app", "db", "uid", "https://api.example.invalid",
                       tmp_path / "ca", lambda: "test-only", catalog_role=role)


@pytest.mark.parametrize("kind", registration_kinds.KINDS)
@pytest.mark.parametrize("role", registration_kinds.ROLES)
def test_every_kind_takes_either_role_from_the_catalog(tmp_path, kind, role):
    env = EnvironmentRef(TENANT, "env", kind, "app", "db", "uid", "https://api.example.invalid",
                         tmp_path / "ca", lambda: "test-only", catalog_role=role)
    assert env.role == role


def test_role_default_is_legacy_only_and_deprecated(tmp_path):
    args = (TENANT, "env")
    rest = ("app", "db", "uid", "https://api.example.invalid", tmp_path / "ca", lambda: "test-only")
    with pytest.warns(DeprecationWarning):
        assert EnvironmentRef(*args, "aws", *rest).role == "source"
    with pytest.warns(DeprecationWarning):
        assert EnvironmentRef(*args, "onprem", *rest).role == "target"
    with pytest.raises(ValueError, match="environment_role_required"):
        EnvironmentRef(*args, "gcp", *rest)
    with pytest.raises(ValueError, match="invalid_environment_reference"):
        EnvironmentRef(*args, "azure", *rest, catalog_role="source")


def test_gcp_environment_registers_with_its_catalog_role(registered):
    f = registered
    for ref, role in ((SRC["connection_ref"], "source"), (DST["connection_ref"], "target")):
        key = (TENANT, ref)
        f.registry.environments[key] = replace(f.registry.environments[key], kind="gcp", catalog_role=role)
    created = f.post("environments", SRC | {"kind": "gcp"}, "gcp-src")
    assert created.status_code == 201 and created.json()["kind"] == "gcp" and created.json()["role"] == "source"
    target = f.post("environments", DST | {"kind": "gcp"}, "gcp-dst")
    assert target.status_code == 201 and target.json()["role"] == "target"
    assert f.post("environments", SRC | {"kind": "azure"}, "azure").status_code == 422


def test_aws_target_is_registered_but_pair_needs_ready_adapters(registered):
    f = registered
    ref = (TENANT, DST["connection_ref"])
    f.registry.environments[ref] = replace(f.registry.environments[ref], kind="aws", catalog_role="target",
                                           cluster_uid="cluster-b")
    src_ref = (TENANT, SRC["connection_ref"])
    f.registry.environments[src_ref] = replace(f.registry.environments[src_ref], catalog_role="source",
                                               cluster_uid="cluster-a")
    assert f.post("environments", SRC, "src-key").status_code == 201
    target = f.post("environments", DST | {"kind": "aws"}, "dst-key")
    assert target.status_code == 201 and target.json()["role"] == "target"
    app = f.post("workloads", APP, "app-key").json()
    body = {"workload_id": app["workload_id"], "target_environment_id": "env-dst", **PROFILE}
    f.probe.results.clear()
    refused = f.post("plans", body, "plan-key")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "adapter_missing:target.rehearsal"
    assert f.probe.results == []
    all_slots_ready(f, "aws")
    assert f.post("plans", body, "plan-key2").status_code == 201


@pytest.mark.parametrize(("source_kind", "target_kind", "code"), [
    ("aws", "onprem", None),
    ("gcp", "onprem", None),
    ("onprem", "onprem", None),
    ("aws", "aws", "target.rehearsal"),
    ("aws", "gcp", "target.rehearsal"),
    ("gcp", "aws", "target.rehearsal"),
    ("gcp", "gcp", "target.rehearsal"),
    ("onprem", "aws", "target.rehearsal"),
    ("onprem", "gcp", "target.rehearsal"),
])
def test_default_readiness_follows_the_implemented_slots(source_kind, target_kind, code):
    # A3-A10 fill every slot for every kind except target.rehearsal, which A5 has
    # only for onprem (aws and gcp report UNSUPPORTED).
    readiness = registration_kinds.adapter_readiness()
    assert registration_kinds.missing_slot(readiness, source_kind, target_kind) == code


def test_partial_target_readiness_names_the_missing_slot():
    slots = [s for s in registration_kinds.SLOTS if s != "target.rehearsal"]
    readiness = registration_kinds.adapter_readiness({"gcp": slots})
    assert registration_kinds.missing_slot(readiness, "aws", "gcp") == "target.rehearsal"
    readiness = registration_kinds.adapter_readiness({"gcp": [s for s in slots if s != "both.identity"]})
    assert registration_kinds.missing_slot(readiness, "gcp", "onprem") == "both.identity"


@pytest.mark.parametrize("override", [{"azure": []}, {"aws": ["target.unknown"]}, {"aws": "source.static_rules"},
                                      {"aws": ["both.identity", "both.identity"]}, []])
def test_invalid_readiness_override_is_rejected(override):
    with pytest.raises(ValueError, match="invalid_adapter_readiness"):
        registration_kinds.adapter_readiness(override)


@pytest.mark.parametrize("kind", registration_kinds.KINDS)
def test_same_kind_plan_needs_distinct_cluster_identity(registered, kind):
    f = registered
    source, target = same_kind(f, kind, source_cluster="shared", target_cluster="shared")
    all_slots_ready(f, kind)
    body = register_pair(f, source, target)
    refused = f.post("plans", body, "plan-key")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "same_cluster_identity"


@pytest.mark.parametrize("kind", ["aws", "gcp"])
def test_same_kind_plan_without_cluster_identity_is_refused(registered, kind):
    f = registered
    source, target = same_kind(f, kind, source_cluster=None)
    all_slots_ready(f, kind)
    body = register_pair(f, source, target)
    refused = f.post("plans", body, "plan-key")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "cluster_identity_unverified"


def test_onprem_same_kind_plan_keeps_the_t10_rule_without_cluster_identity(registered):
    """e38c5983: on-prem -> on-prem on distinct databases was accepted before cluster identity."""
    f = registered
    source, target = same_kind(f, "onprem", source_cluster=None, target_cluster=None)
    body = register_pair(f, source, target)
    created = f.post("plans", body, "plan-key")
    assert created.status_code == 201 and created.json()["state"] == "COLLECT"


def test_same_kind_plan_with_distinct_clusters_and_databases_is_created(registered):
    f = registered
    source, target = same_kind(f, "aws")
    all_slots_ready(f, "aws")
    body = register_pair(f, source, target)
    created = f.post("plans", body, "plan-key")
    assert created.status_code == 201 and created.json()["state"] == "COLLECT"


def test_registered_cluster_identity_must_not_change(registered):
    f = registered
    source, target = same_kind(f, "aws")
    all_slots_ready(f, "aws")
    body = register_pair(f, source, target)
    key = (TENANT, DST["connection_ref"])
    f.registry.environments[key] = replace(f.registry.environments[key], cluster_uid="rebuilt")
    refused = f.post("plans", body, "plan-key")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "environment_changed"


def test_kind_constants_match_the_contract():
    contract = json.loads((Path(__file__).parents[3] / "docs/contracts/environment-kinds.json").read_text())
    assert set(registration_kinds.KINDS) == set(contract["kinds"])
    assert list(registration_kinds.ROLES) == contract["roles"]
    assert list(registration_kinds.SLOTS) == list(contract["slots"])
    assert registration_kinds.REJECTION_PREFIX == contract["plan_rejection_prefix"]


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
                  evaluation_profiles=[dict(tenant_id=TENANT, **PROFILE)],
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
        plan = dict(workload_id="missing-app", target_environment_id="missing-target", **PROFILE)
        # The real host loads its trusted profile policy: only the allowed pair
        # can reach registration lookup; a caller cannot select a weaker profile.
        assert client.post("/v1/plans", json=plan, headers=headers).status_code == 404
        denied = client.post("/v1/plans", json=plan | {"profile_id": "demo-gate"}, headers=headers)
        assert denied.json()["detail"]["code"] == "evaluation_profile_unavailable"
        assert client.post(url, json=SRC, headers=list(headers.items()) + [("Idempotency-Key", "second")]).status_code == 422
        grants.write_text("[]")
        assert client.post(url, json=SRC, headers=headers).status_code == 403
