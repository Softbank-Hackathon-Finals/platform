"""Exercise the execution store with the production DML-only runtime grants."""

import secrets
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from bravo_cutover.contract import CutoverRequest, Status
from bravo_cutover.provision import state_runtime_grants_sql
from bravo_cutover.store import Store


def test_migrator_can_reapply_its_schema_without_database_create(postgres_dsn):
    password = secrets.token_urlsafe(24)
    with psycopg.connect(postgres_dsn, autocommit=True) as admin:
        admin.execute("DROP SCHEMA IF EXISTS executor CASCADE")
        admin.execute(sql.SQL("CREATE ROLE cutover_executor_migration LOGIN PASSWORD {}").format(sql.Literal(password)))
        admin.execute("CREATE SCHEMA executor AUTHORIZATION cutover_executor_migration")
        params = conninfo_to_dict(postgres_dsn) | {"user": "cutover_executor_migration", "password": password}
        migrator = Store(make_conninfo(**params))
        try:
            assert admin.execute("SELECT has_database_privilege('cutover_executor_migration',current_database(),'CREATE')").fetchone()[0] is False
            migrator.migrate()
            migrator.migrate()
            assert admin.execute("SELECT tableowner FROM pg_tables WHERE schemaname='executor' AND tablename='executions'").fetchone()[0] == "cutover_executor_migration"
        finally:
            migrator.close()
            admin.execute("DROP OWNED BY cutover_executor_migration")
            admin.execute("DROP ROLE cutover_executor_migration")


def test_runtime_can_record_and_recover_execution_but_cannot_migrate(store, postgres_dsn):
    password = secrets.token_urlsafe(24)
    with psycopg.connect(postgres_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE cutover_executor_runtime LOGIN PASSWORD {}").format(sql.Literal(password)))
        admin.execute(state_runtime_grants_sql())
        params = conninfo_to_dict(postgres_dsn) | {"user": "cutover_executor_runtime", "password": password}
        runtime = Store(make_conninfo(**params))
        try:
            request = CutoverRequest(plan_id="runtime", epoch=1,
                source={"environment_id": "env-aws", "connection_ref": "cutover/source"},
                target={"environment_id": "env-onprem", "connection_ref": "cutover/target"},
                write_pause_budget_ms=5000, deadline=datetime.now(UTC) + timedelta(minutes=1))
            execution, created = runtime.create("runtime-test", request, ("source/chat", "target/chat"))
            assert created and runtime.acquire_singleton()
            assert runtime.claim(execution.execution_id)
            assert runtime.owns(execution.execution_id)
            assert runtime.record_step(execution.execution_id, "compare", "succeeded")
            assert runtime.record_receipt(execution.execution_id, {"verdict": "MATCH"})
            assert runtime.mark(execution.execution_id, "committed_at")
            assert runtime.finish(execution.execution_id, Status.DONE, {"outcome": "DONE"})
            assert runtime.find("runtime", 1)[0].result["receipt"]["verdict"] == "MATCH"
            assert runtime.unfinished() == []
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime.migrate()
            with psycopg.connect(make_conninfo(**params), autocommit=True) as conn:
                for statement in ("CREATE TABLE executor.extra(id int)",
                                  "DELETE FROM executor.executions", "DROP SCHEMA executor CASCADE"):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        conn.execute(statement)
        finally:
            runtime.close()
            admin.execute("DROP OWNED BY cutover_executor_runtime")
            admin.execute("DROP ROLE cutover_executor_runtime")
