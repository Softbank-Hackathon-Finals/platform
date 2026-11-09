"""Actual T10 SQLite/PostgreSQL; synthetic manager, producer and current reader."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from bravo_platform_events.gate_records import GateRecords
from bravo_platform_events.plan_api import Principal
from bravo_platform_events.store import EventError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_readiness import ready  # noqa: F401
from test_stored_services import stored, stored_postgres  # noqa: F401

from bravo_gate.contract import GateError, canonical, time
from bravo_gate.key_records import PlanAuthorityKeys, _key_ref
from bravo_gate.signatures import AuthorityKey


@pytest.fixture
def key_registry(stored):  # noqa: F811
    f = stored
    manager = Principal("fixture-key-manager", f.principal.tenant_id)
    producer = Principal("fixture-producer", f.principal.tenant_id)
    grants = {
        manager.subject_id: {"gate:manage-provenance"},
        producer.subject_id: {"gate:read"},
        f.principal.subject_id: {"gate:read", "gate:record", "gate:bind"},
    }
    state = {"now": f.state["now"], "hook": lambda *_: None}

    def authorize(c, actor, plan, action):
        state["hook"](action)
        return (
            actor.tenant_id == f.principal.tenant_id
            and plan["plan_id"] == f.plan.plan_id
            and action in grants.get(actor.subject_id, set())
        )

    records = GateRecords(f.store, authorize=authorize)
    f.records = records
    f.prepare()
    registry = PlanAuthorityKeys(records, clock=lambda: state["now"])

    def fresh_key():
        material = Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        return AuthorityKey(
            material,
            (manager.tenant_id,),
            ("context",),
            (state["now"] - timedelta(seconds=60)).isoformat(),
            (state["now"] + timedelta(seconds=60)).isoformat(),
            False,
        )

    def enroll(key, key_id="key-one", actor=None, subject=None, revision=0):
        return registry.enroll(
            actor or manager,
            f.plan.plan_id,
            issuer_id="fixture-issuer",
            key_id=key_id,
            producer_subject_id=subject or producer.subject_id,
            key=key,
            expected_revision=revision,
        )

    def read(key_id="key-one", *, chosen=registry, who=None):
        with f.store._transaction(write=True, plan_id=f.plan.plan_id) as c:
            plan = dict(
                c.execute("SELECT * FROM plans WHERE plan_id=?", (f.plan.plan_id,)).fetchone()
            )
            return chosen.read_key(
                records.view(c, who or f.principal, plan), "fixture-issuer", key_id
            )

    return SimpleNamespace(
        f=f,
        manager=manager,
        producer=producer,
        records=records,
        registry=registry,
        state=state,
        grants=grants,
        fresh_key=fresh_key,
        enroll=enroll,
        read=read,
    )


def test_enroll_read_clamps_trust_start_and_preserves_plan_state(key_registry):
    r = key_registry
    before = r.f.store.snapshot(r.f.plan.plan_id)
    key = r.fresh_key()
    receipt = r.enroll(key)
    assert r.enroll(key) == receipt
    current = r.read()
    assert current == replace(key, valid_from=receipt["enrolled_at"])
    assert set(receipt["key"]) == set(asdict(key))
    assert r.read("not-enrolled") is None
    assert r.f.store.snapshot(r.f.plan.plan_id) == before


def test_revocation_is_irreversible_append_only_and_replayable(key_registry):
    r = key_registry
    key = r.fresh_key()
    first = r.enroll(key)
    revoked = r.registry.revoke(
        r.manager,
        r.f.plan.plan_id,
        issuer_id="fixture-issuer",
        key_id="key-one",
        expected_revision=1,
    )
    assert revoked["revision"] == 2 and r.read().revoked is True
    for revision in (1, 2):
        assert (
            r.registry.revoke(
                r.manager,
                r.f.plan.plan_id,
                issuer_id="fixture-issuer",
                key_id="key-one",
                expected_revision=revision,
            )
            == revoked
        )
    assert first["key"]["revoked"] is False
    with pytest.raises(GateError, match="AUTHORITY_KEY_CONFLICT"):
        r.enroll(key)
    with pytest.raises(GateError, match="REUSE_OR_BUDGET"):
        r.enroll(key, key_id="alias-for-revoked-material")
    replacement = r.fresh_key()
    r.enroll(replacement, key_id="new-material")
    assert r.read("new-material").public_key_hex == replacement.public_key_hex
    assert r.read().revoked is True


@pytest.mark.parametrize(
    "fault",
    [
        "self",
        "tenant",
        "purpose",
        "revoked",
        "expired",
        "boolean",
        "nonzero",
        "other-manager",
        "reader",
        "producer",
    ],
)
def test_enrollment_requires_distinct_manager_exact_scope_and_explicit_revision(
    key_registry, fault
):
    r = key_registry
    key = r.fresh_key()
    options = {}
    if fault == "self":
        options["subject"] = r.manager.subject_id
    if fault == "tenant":
        key = replace(key, tenants=("another-tenant",))
    if fault == "purpose":
        key = replace(key, purposes=("context", "run-source"))
    if fault == "revoked":
        key = replace(key, revoked=True)
    if fault == "expired":
        key = replace(key, valid_until=(r.state["now"] - timedelta(seconds=1)).isoformat())
    if fault == "boolean":
        options["revision"] = False
    if fault == "nonzero":
        options["revision"] = 1
    if fault == "other-manager":
        options["actor"] = replace(r.manager, tenant_id="another-tenant")
    if fault == "reader":
        options["actor"] = r.f.principal
    if fault == "producer":
        options["actor"] = r.producer
    with pytest.raises(GateError):
        r.enroll(key, **options)


@pytest.mark.parametrize("revision", [False, 0, -1, 3, "1", 2])
def test_revocation_revision_cannot_skip_current_head(key_registry, revision):
    r = key_registry
    r.enroll(r.fresh_key())
    with pytest.raises(GateError, match="REVISION_CONFLICT"):
        r.registry.revoke(
            r.manager,
            r.f.plan.plan_id,
            issuer_id="fixture-issuer",
            key_id="key-one",
            expected_revision=revision,
        )
    assert r.read().revoked is False


def test_two_current_enrollments_cannot_replace_each_other(key_registry):
    r = key_registry
    keys = [r.fresh_key(), r.fresh_key()]

    def competing(key):
        try:
            return r.enroll(key)
        except GateError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = [
            future.result(timeout=10) for future in [workers.submit(competing, key) for key in keys]
        ]
    assert sum(isinstance(value, dict) for value in results) == 1
    assert results.count("AUTHORITY_KEY_CONFLICT") == 1
    assert r.read().public_key_hex in {key.public_key_hex for key in keys}


def test_management_revoked_at_final_check_rolls_back_insert(key_registry):
    r = key_registry
    count = {"manage": 0}

    def revoke(action):
        if action == "gate:manage-provenance":
            count["manage"] += 1
            if count["manage"] == 2:
                r.grants[r.manager.subject_id].clear()

    r.state["hook"] = revoke
    with pytest.raises(GateError, match="AUTHORITY_FORBIDDEN"):
        r.enroll(r.fresh_key())
    r.state["hook"] = lambda *_: None
    assert r.read() is None


def test_clock_regression_rolls_back_enrollment(key_registry):
    r = key_registry

    def regress(action):
        if action == "gate:manage-provenance":
            r.state["now"] -= timedelta(seconds=1)

    r.state["hook"] = regress
    with pytest.raises(GateError, match="CLOCK_REGRESSION"):
        r.enroll(r.fresh_key())
    r.state["hook"] = lambda *_: None
    assert r.read() is None


@pytest.mark.parametrize("fault", ["purpose", "self", "expired-trust"])
def test_initial_record_is_revalidated_when_read(key_registry, fault):
    r = key_registry
    first = r.enroll(r.fresh_key())
    value = first | {"key_id": "unqualified-key"}
    if fault == "purpose":
        value["key"] = first["key"] | {"purposes": ["context", "gate-result"]}
    if fault == "self":
        value["producer_subject_id"] = value["enrolled_by"]
    if fault == "expired-trust":
        value["enrolled_at"] = first["key"]["valid_until"]
    with r.f.store._transaction(write=True, plan_id=r.f.plan.plan_id) as c:
        c.execute(
            "INSERT INTO plan_commands VALUES (?,?,?,?)",
            (
                r.f.plan.plan_id,
                _key_ref("fixture-issuer", "unqualified-key") + "1",
                "{}",
                canonical(value).decode(),
            ),
        )
    with pytest.raises(GateError, match="AUTHORITY_RECORD_INVALID"):
        r.read("unqualified-key")
    with pytest.raises(GateError, match="AUTHORITY_RECORD_INVALID"):
        r.enroll(r.fresh_key(), key_id="new-after-invalid-inventory")


@pytest.mark.parametrize("fault", ["tenant", "material", "revision", "time"])
def test_invalid_revocation_record_cannot_be_current_key(key_registry, fault):
    r = key_registry
    first = r.enroll(r.fresh_key())
    record = first | {
        "revision": 2,
        "key": first["key"] | {"revoked": True},
        "revoked_at": r.state["now"].isoformat(),
        "revoked_by": r.manager.subject_id,
    }
    if fault == "tenant":
        record["tenant_id"] = "other-tenant"
    if fault == "material":
        record["key"] = record["key"] | {"public_key_hex": "b" * 64}
    if fault == "revision":
        record["revision"] = True
    if fault == "time":
        record["revoked_at"] = (time(first["enrolled_at"]) - timedelta(seconds=1)).isoformat()
    with r.f.store._transaction(write=True, plan_id=r.f.plan.plan_id) as c:
        c.execute(
            "INSERT INTO plan_commands VALUES (?,?,?,?)",
            (
                r.f.plan.plan_id,
                _key_ref("fixture-issuer", "key-one") + "2",
                "{}",
                canonical(record).decode(),
            ),
        )
    with pytest.raises(GateError, match="AUTHORITY_RECORD_INVALID"):
        r.read()


def test_current_read_grant_and_same_plan_view_are_required(key_registry):
    r = key_registry
    r.enroll(r.fresh_key())
    r.grants[r.f.principal.subject_id].clear()
    with pytest.raises(EventError, match="forbidden"):
        r.read()
    with pytest.raises(GateError, match="SERVER_RECORD_VIEW_REQUIRED"):
        r.registry.read_key(SimpleNamespace(), "fixture-issuer", "key-one")


def test_another_records_policy_cannot_supply_the_registry_view(key_registry):
    r = key_registry
    r.enroll(r.fresh_key())
    foreign = GateRecords(r.f.store, authorize=lambda *_: True)
    with r.f.store._transaction(write=True, plan_id=r.f.plan.plan_id) as c:
        plan = dict(
            c.execute("SELECT * FROM plans WHERE plan_id=?", (r.f.plan.plan_id,)).fetchone()
        )
        view = foreign.view(c, r.f.principal, plan)
        with pytest.raises(GateError, match="SERVER_RECORD_VIEW_REQUIRED"):
            r.registry.read_key(view, "fixture-issuer", "key-one")


def test_a_view_cannot_be_reused_after_its_transaction_ends(key_registry):
    r = key_registry
    r.enroll(r.fresh_key())
    with r.f.store._transaction(write=True, plan_id=r.f.plan.plan_id) as c:
        plan = dict(
            c.execute("SELECT * FROM plans WHERE plan_id=?", (r.f.plan.plan_id,)).fetchone()
        )
        view = r.records.view(c, r.f.principal, plan)
    with pytest.raises(GateError, match="AUTHORITY_VIEW_UNAVAILABLE"):
        r.registry.read_key(view, "fixture-issuer", "key-one")


def test_key_read_diagnostics_do_not_escape_the_port(key_registry, monkeypatch):
    r = key_registry

    def broken(*_):
        raise RuntimeError("password=fixture-private-value")

    monkeypatch.setattr(r.registry, "_head", broken)
    with pytest.raises(GateError) as error:
        r.read()
    assert str(error.value) == "AUTHORITY_STORE_UNAVAILABLE"
    assert "private-value" not in str(error.value)


def test_database_diagnostics_do_not_escape_trusted_port(key_registry):
    r = key_registry

    class Broken:
        @contextmanager
        def _transaction(self, **_):
            raise RuntimeError("password=fixture-private-value")
            yield

    ledger = PlanAuthorityKeys(
        GateRecords(Broken(), authorize=lambda *_: False), clock=lambda: r.state["now"]
    )
    with pytest.raises(GateError) as error:
        ledger.enroll(
            r.manager,
            r.f.plan.plan_id,
            issuer_id="fixture-issuer",
            key_id="new-key",
            producer_subject_id=r.producer.subject_id,
            key=r.fresh_key(),
            expected_revision=0,
        )
    assert str(error.value) == "AUTHORITY_STORE_UNAVAILABLE"
    assert "private-value" not in str(error.value)
