"""PostgreSQL adapter for the existing state/event contract, not a hosted service."""

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from .contract import validate_identifier
from .plans import PlanStore


class StorageUnavailable(RuntimeError):
    """Safe public error; driver diagnostics may contain private connection data."""


class _Queries:
    """Execute only the internal store's fixed qmark SQL with bound parameters.

    This is not a general SQL translator: these queries contain no literal '?',
    percent escapes or dynamic identifiers. Values stay separate from SQL.
    """

    def __init__(self, connection):
        self.connection = connection

    def execute(self, query, parameters=()):
        return self.connection.execute(query.replace("?", "%s"), parameters)


class PostgresPlanStore(PlanStore):
    """Use a pre-migrated DB and runtime credentials; never runs schema DDL.

    Calls use separate connections. Every writer must use this adapter's per-plan
    lock protocol. The trusted runtime role is not a tenant-facing SQL account.
    """

    def __init__(self, conninfo: str):
        self._conninfo = conninfo
        with self._transaction() as c:
            versions = c.execute("SELECT version FROM schema_version").fetchall()
            if versions not in ([{"version": 1}], [{"version": 2}]):
                raise StorageUnavailable("unsupported_storage_schema")

    @contextmanager
    def _transaction(self, *, write: bool = False, plan_id: str | None = None):
        if write:
            validate_identifier(plan_id)
        try:
            with psycopg.connect(
                self._conninfo, autocommit=True, row_factory=dict_row, connect_timeout=5
            ) as c:
                with c.transaction():
                    # Reads need one snapshot across plan, stream and retained events.
                    # Writers need a fresh snapshot AFTER waiting for their plan lock.
                    c.execute(
                        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ WRITE"
                        if write
                        else "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                    c.execute("SET LOCAL search_path = pg_catalog, bravo_platform, pg_temp")
                    c.execute("SET LOCAL lock_timeout = '5s'")
                    c.execute("SET LOCAL statement_timeout = '10s'")
                    if write:
                        # Includes first registration (no row exists to lock yet).
                        # Hash collisions only serialize unrelated plans; no data mixing.
                        c.execute(
                            "SELECT pg_catalog.pg_advisory_xact_lock("
                            "pg_catalog.hashtextextended(%s, 0))",
                            (plan_id,),
                        )
                    yield _Queries(c)
        except psycopg.Error:
            # Do not expose DSN, server names, query values or PostgreSQL DETAIL.
            # Commit acknowledgement loss is ambiguous: retry identical command/key.
            raise StorageUnavailable("storage_unavailable") from None
