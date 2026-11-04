"""Synthetic PostgreSQL fixture for the isolated cloud validation container only.

Replaces the process-owning local fixture in the transmitted test bundle, not
the product package. BRAVO_TEST_SOCKET must name this run's private Unix socket.
The privileged outer harness owns the container lifecycle; tests get no Docker,
AWS or Kubernetes credentials. PostgreSQL restart is checked by that harness.
"""

import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

SQL = Path(__file__).parents[1] / "sql"


class TemporaryPostgres:
    def __init__(self):
        self.counter = 0
        self.socket = os.environ["BRAVO_TEST_SOCKET"]
        if not self.socket.startswith("/tmp/bravo-t10-window-"):
            raise ValueError("isolated_socket_required")
        with self.connect() as c:
            self.version = c.execute("SHOW server_version").fetchone()[0]
            if not self.version.startswith("18.4"):
                raise RuntimeError("expected_postgres_18_4")
            c.execute("CREATE DATABASE platform_template")
        with self.connect("platform_template") as c:
            c.execute((SQL / "bootstrap_roles.sql").read_text())
            c.execute("CREATE ROLE fixture_migration LOGIN IN ROLE bravo_platform_migrator")
            c.execute("CREATE ROLE fixture_runtime LOGIN IN ROLE bravo_platform_runtime")
        with self.connect("platform_template", "fixture_migration") as c:
            c.execute((SQL / "0001_state_event.sql").read_text())

    def conninfo(self, database="postgres", user=None):
        return make_conninfo(host=self.socket, dbname=database, user=user or "postgres")

    def connect(self, database="postgres", user=None):
        return psycopg.connect(self.conninfo(database, user), autocommit=True)

    def database(self):
        self.counter += 1
        database = f"contract_{self.counter}"
        with self.connect() as c:
            c.execute(sql.SQL("CREATE DATABASE {} TEMPLATE platform_template").format(sql.Identifier(database)))
        return database

    def restart(self):
        raise RuntimeError("restart_is_owned_by_outer_harness")

    def close(self):
        pass  # The outer harness verifies removal of its exact container and tmpfs.
