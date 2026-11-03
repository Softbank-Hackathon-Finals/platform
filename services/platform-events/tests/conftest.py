from contextlib import contextmanager

import pytest
from postgres_fixture import TemporaryPostgres

from bravo_platform_events.plans import PlanStore
from bravo_platform_events.postgres import PostgresPlanStore, StorageUnavailable


@pytest.fixture(scope="session")
def postgres_cluster():
    cluster = TemporaryPostgres()
    yield cluster
    cluster.close()


class Harness:
    def __init__(self, kind, tmp_path, cluster=None):
        self.kind = kind
        self.cluster = cluster
        if cluster:
            self.database = cluster.database()
            self.conninfo = cluster.conninfo(self.database, "fixture_runtime")
        else:
            self.path = tmp_path / "contract.db"
        self.store = self.reopen()

    def reopen(self):
        if self.cluster:
            return PostgresPlanStore(self.conninfo)
        return PlanStore(self.path)

    @contextmanager
    def admin(self):
        if self.cluster:
            with self.cluster.connect(self.database) as c:
                c.execute("SET search_path = pg_catalog, bravo_platform")
                yield c
        else:
            with self.store._connection() as c:
                yield c

    @contextmanager
    def reject_insert(self, table):
        assert table in ("events", "plan_commands")
        with self.admin() as c:
            if self.cluster:
                c.execute(f"REVOKE INSERT ON {table} FROM bravo_platform_runtime")
            else:
                c.execute(
                    f"CREATE TRIGGER fault BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected'); END"
                )
        try:
            yield
        finally:
            with self.admin() as c:
                if self.cluster:
                    c.execute(f"GRANT INSERT ON {table} TO bravo_platform_runtime")
                else:
                    c.execute("DROP TRIGGER fault")

    @property
    def storage_error(self):
        import sqlite3

        return StorageUnavailable if self.cluster else sqlite3.IntegrityError


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, tmp_path):
    cluster = request.getfixturevalue("postgres_cluster") if request.param == "postgres" else None
    return Harness(request.param, tmp_path, cluster)


@pytest.fixture
def pg_backend(tmp_path, postgres_cluster):
    return Harness("postgres", tmp_path, postgres_cluster)
