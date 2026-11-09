"""Real PostgreSQL durability, isolation and migration/runtime-role boundaries."""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from test_plans import SPEC, complete, progress
from test_storage_contract import register

from bravo_platform_events.postgres import PostgresPlanStore, StorageUnavailable


def test_database_crash_restart_preserves_receipt_sequence_and_tenant(pg_backend):
    store = pg_backend.store
    register(store)
    receipt = complete(store)
    progress(store)
    snapshot = store.snapshot(SPEC.plan_id)
    pg_backend.cluster.restart()
    reopened = pg_backend.reopen()
    assert reopened.snapshot(SPEC.plan_id) == snapshot
    assert complete(reopened) == receipt
    assert reopened.tenant_for(SPEC.plan_id) == SPEC.tenant_id
    assert [e["sequence"] for e in reopened.read_after(SPEC.plan_id, None)] == [1, 2, 3]
    assert progress(reopened, "after-restart")["sequence"] == 4


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE bravo_platform.forbidden (id INTEGER)",
        "CREATE TABLE public.forbidden (id INTEGER)",
        "CREATE SCHEMA forbidden",
        "ALTER TABLE bravo_platform.plans ADD COLUMN forbidden TEXT",
        "UPDATE bravo_platform.plans SET tenant_id = 'other'",
        "UPDATE bravo_platform.plans SET spec_json = '{}'",
        "UPDATE bravo_platform.events SET event_json = '{}'",
        "DELETE FROM bravo_platform.events",
        "DELETE FROM bravo_platform.plan_commands",
        "UPDATE bravo_platform.schema_version SET version = 1",
        "SET ROLE bravo_platform_migrator",
        "GRANT bravo_platform_migrator TO fixture_runtime",
    ],
)
def test_runtime_role_cannot_migrate_rebind_or_rewrite_history(pg_backend, statement):
    register(pg_backend.store)
    with pg_backend.cluster.connect(pg_backend.database, "fixture_runtime") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(statement)
    assert pg_backend.store.tenant_for(SPEC.plan_id) == SPEC.tenant_id
    assert len(pg_backend.store.read_after(SPEC.plan_id, None)) == 1


def test_runtime_cannot_read_unrelated_schema(pg_backend):
    with pg_backend.admin() as c:
        c.execute("CREATE SCHEMA unrelated")
        c.execute("CREATE TABLE unrelated.private_data (id INTEGER)")
    with pg_backend.cluster.connect(pg_backend.database, "fixture_runtime") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT * FROM unrelated.private_data")


def test_runtime_adapter_fails_closed_on_missing_or_unknown_schema(pg_backend):
    with pg_backend.admin() as c:
        c.execute("DELETE FROM schema_version")
    with pytest.raises(StorageUnavailable, match="^unsupported_storage_schema$"):
        pg_backend.reopen()
    with pg_backend.admin() as c:
        c.execute("ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check")
        c.execute("INSERT INTO schema_version VALUES (3)")
    with pytest.raises(StorageUnavailable, match="^unsupported_storage_schema$"):
        pg_backend.reopen()
    with pg_backend.admin() as c:
        c.execute("ALTER TABLE schema_version RENAME TO hidden_version")
    with pytest.raises(StorageUnavailable, match="^storage_unavailable$"):
        pg_backend.reopen()


def test_storage_failure_message_omits_server_query_and_values(pg_backend):
    register(pg_backend.store)
    with pg_backend.reject_insert("events"), pytest.raises(StorageUnavailable) as error:
        progress(pg_backend.store)
    assert str(error.value) == "storage_unavailable"
    assert error.value.__suppress_context__ is True
    assert len(pg_backend.store.read_after(SPEC.plan_id, None)) == 1


def test_plan_lock_does_not_block_an_unrelated_plan(pg_backend):
    store = pg_backend.store
    register(store)
    store.register_stream("unrelated")
    with store._transaction(write=True, plan_id=SPEC.plan_id):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                store.append,
                plan_id="unrelated",
                run_id="run-1",
                producer_key="event-1",
                event_type="execution.updated",
                occurred_at="2026-11-03T10:00:00Z",
                data={"execution_id": "fixture-1", "step": "collect", "status": "succeeded"},
            )
            assert future.result(timeout=3)["sequence"] == 1


def test_writer_uses_read_committed_after_waiting_for_lock(pg_backend):
    # A host's DSN default must not silently change the adapter's isolation contract.
    store = PostgresPlanStore(
        pg_backend.conninfo + " options='-c default_transaction_isolation=serializable'"
    )
    register(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _: complete(store), range(16)))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert len(store.read_after(SPEC.plan_id, None)) == 2
