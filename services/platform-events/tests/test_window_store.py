"""Real SQLite/PostgreSQL storage with the pinned, unmodified T36 intake library."""

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier, Event

import psycopg
import pytest
from bravo_observability.intake import IntakeRejected
from bravo_observability.sli import judge
from test_plans import SPEC, complete

from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal
from bravo_platform_events.postgres import StorageUnavailable
from bravo_platform_events.store import EventError
from bravo_platform_events.window_store import ProducerIdentity, WindowIntake

ROOT = Path(__file__).parents[3]
SQL = Path(__file__).parents[1] / "sql/0002_sli_windows.sql"
START = "2026-11-08T05:31:00Z"
NOW = "2026-11-08T05:31:21Z"
EXPIRES = "2026-11-08T08:00:00Z"
PRODUCER = ProducerIdentity("loadgen-fixture", SPEC.tenant_id, SPEC.plan_id,
                            "fixture-credential-v1", EXPIRES)


def window(start="2026-11-08T05:31:10Z", end="2026-11-08T05:31:20Z"):
    result = json.loads((ROOT / "docs/observability/examples/window-shift-10.json").read_text())
    result["window"] = dict(start=start, end=end)
    result["received_at"] = "2000-01-01T00:00:00Z"
    return result


class Fixture:
    def __init__(self, backend):
        self.backend = backend
        if backend.cluster:
            with backend.cluster.connect(backend.database, "fixture_migration") as c:
                c.execute(SQL.read_text())
        backend.store.register_plan(SPEC, run_id="fixture", occurred_at=START)
        self.now = NOW
        self.intake = self.reopen()

    def reopen(self):
        return WindowIntake(self.backend.reopen(), clock=lambda: self.now)

    def bind(self, *, producer=PRODUCER, enabled=True, revision=0, key=None):
        return self.intake.set_binding(producer, enabled=enabled, expected_revision=revision,
            administration_ref="fixture-admin", command_key=key or f"binding-{revision}")

    def raw_latest(self):
        with self.backend.store._transaction() as c:
            return self.intake.latest_in_transaction(c, SPEC.plan_id)


@pytest.fixture
def fixture(backend):
    return Fixture(backend)


def test_default_denial_no_implicit_plan_or_producer_enrollment(fixture):
    for identity in (None, PRODUCER, {"subject_id": PRODUCER.subject_id}):
        with pytest.raises(EventError, match="forbidden"):
            fixture.intake.receive(identity, window())
    assert fixture.raw_latest() is None


def test_received_at_overwritten_reopen_and_real_upstream_judgement(fixture):
    fixture.bind()
    document = window()
    original = copy.deepcopy(document)
    accepted = fixture.intake.receive(PRODUCER, document)
    assert document == original
    assert accepted["received_at"] == NOW
    fixture.intake = fixture.reopen()
    stored = fixture.raw_latest()
    assert stored == accepted
    profiles = json.loads((ROOT / "docs/observability/threshold-profiles.json").read_text())
    profile = profiles["profiles"][0]
    args = dict(plan_id=SPEC.plan_id, state="SHIFT", weight_target=10,
                observe_from=original["window"]["start"], prior_unknown=0)
    assert judge(profile, stored, decided_at=NOW, **args)["verdict"] == "PASS"
    later = (datetime.fromisoformat(NOW) + timedelta(minutes=5)).isoformat()
    stale = judge(profile, stored, decided_at=later, **args)
    assert stale["verdict"] == "UNKNOWN" and "STALE" in stale["reasons"]


@pytest.mark.parametrize("change", [
    {"tenant_id": "another-tenant"}, {"subject_id": "another-producer"},
    {"credential_id": "unregistered-credential"}, {"plan_id": "another-plan"},
    {"expires_at": "2026-11-08T05:31:21Z"},
])
def test_identity_plan_tenant_credential_and_expiry_fail_closed(fixture, change):
    fixture.bind()
    with pytest.raises(EventError, match="forbidden"):
        fixture.intake.receive(replace(PRODUCER, **change), window())
    assert fixture.raw_latest() is None


def test_document_cannot_select_another_plan(fixture):
    fixture.bind()
    document = window() | {"plan_id": "other-plan"}
    with pytest.raises(IntakeRejected, match="plan_not_accepting_windows"):
        fixture.intake.receive(PRODUCER, document)
    assert fixture.raw_latest() is None


def test_revocation_rotation_and_old_admin_retry_cannot_reenable(fixture):
    initial = fixture.bind()
    fixture.bind(enabled=False, revision=1)
    assert fixture.bind() == initial
    with pytest.raises(EventError, match="forbidden"):
        fixture.intake.receive(PRODUCER, window())
    rotated = replace(PRODUCER, credential_id="fixture-credential-v2")
    fixture.bind(producer=rotated, revision=2)
    with pytest.raises(EventError, match="forbidden"):
        fixture.intake.receive(PRODUCER, window())
    assert fixture.intake.receive(rotated, window())["received_at"] == NOW


def test_binding_cannot_adopt_a_foreign_plan_or_stale_revision(fixture):
    with pytest.raises(EventError, match="forbidden"):
        fixture.bind(producer=replace(PRODUCER, tenant_id="foreign"))
    fixture.bind()
    with pytest.raises(EventError, match="version_conflict"):
        fixture.bind(key="stale-command")
    with pytest.raises(EventError, match="command_key_conflict"):
        fixture.bind(enabled=False)


def test_changed_plan_needs_new_server_binding(fixture):
    fixture.bind()
    complete(fixture.backend.store)
    with pytest.raises(EventError, match="forbidden"):
        fixture.intake.receive(PRODUCER, window())
    fixture.bind(revision=1)
    fixture.intake.receive(PRODUCER, window())


def test_duplicate_equivalent_instants_order_and_overlap(fixture):
    fixture.bind()
    accepted = fixture.intake.receive(PRODUCER, window())
    attempts = [
        (window("2026-11-08T14:31:10+09:00", "2026-11-08T14:31:20+09:00"), "duplicate"),
        (window("2026-11-08T05:31:00Z", "2026-11-08T05:31:10Z"), "out_of_order"),
        (window("2026-11-08T05:31:15Z", "2026-11-08T05:31:25Z"), "overlapping_window"),
    ]
    fixture.now = "2026-11-08T05:31:31Z"
    for document, code in attempts:
        with pytest.raises(IntakeRejected, match=code):
            fixture.intake.receive(PRODUCER, document)
        assert fixture.raw_latest() == accepted
    adjacent = window("2026-11-08T05:31:20Z", "2026-11-08T05:31:30Z")
    assert fixture.intake.receive(PRODUCER, adjacent)["window"] == adjacent["window"]


def test_multiple_connections_compete_for_the_same_window(fixture):
    fixture.bind()
    barrier = Barrier(8)

    def submit(_):
        intake = fixture.reopen()
        barrier.wait(timeout=10)
        try:
            intake.receive(PRODUCER, window())
            return "stored"
        except IntakeRejected as e:
            return e.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert results.count("stored") == 1 and results.count("duplicate") == 7


def test_distinct_overlapping_windows_cannot_both_commit(fixture):
    fixture.bind()
    fixture.now = "2026-11-08T05:31:31Z"
    barrier = Barrier(2)

    def submit(document):
        intake = fixture.reopen()
        barrier.wait(timeout=10)
        try:
            intake.receive(PRODUCER, document)
            return "stored"
        except IntakeRejected as e:
            return e.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [window(), window("2026-11-08T05:31:15Z", "2026-11-08T05:31:25Z")]))
    assert results.count("stored") == 1
    assert set(results) <= {"stored", "overlapping_window", "out_of_order"}


@pytest.mark.parametrize("change", ["revoke", "plan", "expired"])
def test_waiting_receiver_reads_authority_and_clock_after_plan_lock(fixture, change):
    fixture.bind()
    started = Event()
    waiting = fixture.reopen()

    def receive():
        started.set()
        return waiting.receive(PRODUCER, window())

    # Hold the same write lock, publish an eligibility change, then let the
    # second connection proceed. On PostgreSQL, prove that it is waiting for
    # the advisory lock before publishing the revocation/plan/clock change.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with fixture.backend.store._transaction(write=True, plan_id=SPEC.plan_id) as c:
            future = pool.submit(receive)
            assert started.wait(5)
            if fixture.backend.cluster:
                until = time.monotonic() + 3
                with fixture.backend.cluster.connect(fixture.backend.database) as observer:
                    while not observer.execute(
                        "SELECT count(*) FROM pg_catalog.pg_locks WHERE locktype='advisory' AND NOT granted"
                    ).fetchone()[0]:
                        assert time.monotonic() < until, "receiver did not wait for plan lock"
                        time.sleep(0.01)
            if change == "revoke":
                # Administrative change in this already-owned transaction.
                row = c.execute("SELECT * FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                                (SPEC.plan_id, "window-binding-%")).fetchone()
                receipt = json.loads(row["receipt_json"]) | {"revision": 2, "enabled": False}
                c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                          (SPEC.plan_id, row["command_key"][:-13] + f"{2:013}", row["request_json"], json.dumps(receipt)))
            elif change == "plan":
                c.execute("UPDATE plans SET version=version+1,state='ANALYZE' WHERE plan_id=?", (SPEC.plan_id,))
            else:
                fixture.now = EXPIRES
        with pytest.raises(EventError, match="forbidden"):
            future.result(timeout=10)
    assert fixture.raw_latest() is None


def test_retention_keeps_high_water_mark_and_never_promotes_replayed_pass(fixture):
    fixture.bind()
    fixture.intake.receive(PRODUCER, window())
    fixture.now = "2026-11-08T06:32:01Z"
    newest = window("2026-11-08T06:31:50Z", "2026-11-08T06:32:00Z")
    fixture.intake.receive(PRODUCER, newest)
    with fixture.backend.store._transaction() as c:
        assert c.execute("SELECT count(*) AS n FROM sli_windows").fetchone()["n"] == 1
    with pytest.raises(IntakeRejected, match="out_of_order"):
        fixture.reopen().receive(PRODUCER, window())
    assert fixture.raw_latest()["window"] == newest["window"]


def test_existing_operator_read_authority_is_required(fixture):
    fixture.bind()
    fixture.intake.receive(PRODUCER, window())
    with pytest.raises(EventError, match="forbidden"):
        fixture.intake.latest(PRODUCER, SPEC.plan_id)
    now = int(datetime.fromisoformat(NOW).timestamp())
    human = OperatorPrincipal("human-fixture", SPEC.tenant_id, now, now + 300, now, "session-fixture")
    access = OperatorAccess(fixture.backend.store, clock=lambda: fixture.now)
    access.set_permissions(SPEC.plan_id, human, permissions=["read"], valid_after=NOW,
        expires_at=EXPIRES, expected_revision=0, administration_ref="fixture-admin", command_key="read-grant")
    assert fixture.intake.latest(human, SPEC.plan_id) == fixture.raw_latest()
    access.set_permissions(SPEC.plan_id, human, permissions=[], valid_after=NOW,
        expires_at=EXPIRES, expected_revision=1, administration_ref="fixture-admin", command_key="read-revoke")
    with pytest.raises(EventError, match="forbidden"):
        fixture.intake.latest(human, SPEC.plan_id)


def test_pg_migration_privileges_and_restart(pg_backend):
    fixture = Fixture(pg_backend)
    fixture.bind()
    accepted = fixture.intake.receive(PRODUCER, window())
    with pg_backend.cluster.connect(pg_backend.database, "fixture_runtime") as c:
        for command in ["ALTER TABLE bravo_platform.sli_windows ADD COLUMN forbidden INT",
                        "UPDATE bravo_platform.sli_windows SET document_json='{}'",
                        "DELETE FROM bravo_platform.plan_commands"]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(command)
    pg_backend.cluster.restart()
    fixture.intake = fixture.reopen()  # Reconnect after immediate stop/WAL recovery.
    assert fixture.raw_latest() == accepted
    with pytest.raises(IntakeRejected, match="duplicate"):
        fixture.reopen().receive(PRODUCER, window())


def test_pg_migration_is_atomic_and_existing_v1_keeps_working(pg_backend):
    pg_backend.store.register_plan(SPEC, run_id="fixture", occurred_at=START)
    migration = SQL.read_text().replace("GRANT SELECT, INSERT, DELETE", "SELECT definitely_missing_function();\nGRANT SELECT, INSERT, DELETE")
    with pg_backend.cluster.connect(pg_backend.database, "fixture_migration") as c:
        with pytest.raises(psycopg.Error):
            c.execute(migration)
        c.execute("ROLLBACK")
    with pg_backend.admin() as c:
        assert c.execute("SELECT version FROM schema_version").fetchone()[0] == 1
        assert c.execute("SELECT to_regclass('bravo_platform.sli_windows')").fetchone()[0] is None
    assert pg_backend.reopen().snapshot(SPEC.plan_id)["state"] == "COLLECT"
    with pytest.raises(StorageUnavailable, match="storage_unavailable"):
        with pg_backend.store._transaction() as c:
            WindowIntake.latest_in_transaction(c, SPEC.plan_id)


def test_pg_failed_insert_and_commit_ack_loss_never_return_success(pg_backend):
    from contextlib import contextmanager

    fixture = Fixture(pg_backend)
    fixture.bind()
    with pg_backend.admin() as c:
        c.execute("REVOKE INSERT ON sli_windows FROM bravo_platform_runtime")
    with pytest.raises(StorageUnavailable, match="^storage_unavailable$"):
        fixture.intake.receive(PRODUCER, window())
    assert fixture.raw_latest() is None
    with pg_backend.admin() as c:
        c.execute("GRANT INSERT ON sli_windows TO bravo_platform_runtime")
    actual = fixture.intake.store._transaction

    @contextmanager
    def lost_ack(**kwargs):
        with actual(**kwargs) as c:
            yield c
        raise StorageUnavailable("storage_unavailable")

    fixture.intake.store._transaction = lost_ack
    with pytest.raises(StorageUnavailable):
        fixture.intake.receive(PRODUCER, window())
    assert fixture.raw_latest() is not None
    with pytest.raises(IntakeRejected, match="duplicate"):
        fixture.reopen().receive(PRODUCER, window())
