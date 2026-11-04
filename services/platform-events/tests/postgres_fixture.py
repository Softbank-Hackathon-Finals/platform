"""Private temporary PostgreSQL process; never connects to an existing server."""

import getpass
import shutil
import subprocess
import tempfile
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

SQL = Path(__file__).parents[1] / "sql"


class TemporaryPostgres:
    def __init__(self):
        pg_config = shutil.which("pg_config")
        if pg_config is None:
            raise RuntimeError(
                "PostgreSQL server tools required (install PostgreSQL, then run pytest)"
            )
        self.bin = Path(subprocess.check_output([pg_config, "--bindir"], text=True).strip())
        for name in ("initdb", "pg_ctl", "postgres"):
            if not (self.bin / name).is_file():
                raise RuntimeError(f"PostgreSQL server tool missing: {name}")
        self.temp = tempfile.TemporaryDirectory(prefix="bravo-pg-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.running = False
        self.counter = 0
        try:
            self.run(
                "initdb",
                "-D",
                str(self.data),
                "--auth-local=trust",
                "--auth-host=reject",
                "--no-locale",
                "--encoding=UTF8",
            )
            with (self.data / "postgresql.conf").open("a") as f:
                f.write(f"\nlisten_addresses = ''\nunix_socket_directories = '{self.root}'\n")
            self.start()
            with self.connect() as c:
                c.execute("CREATE DATABASE platform_template")
            with self.connect("platform_template") as c:
                c.execute((SQL / "bootstrap_roles.sql").read_text())
                c.execute("CREATE ROLE fixture_migration LOGIN IN ROLE bravo_platform_migrator")
                c.execute("CREATE ROLE fixture_runtime LOGIN IN ROLE bravo_platform_runtime")
            # Execute DDL using a separate non-superuser migration login.
            with self.connect("platform_template", "fixture_migration") as c:
                c.execute((SQL / "0001_state_event.sql").read_text())
            with self.connect() as c:
                self.version = c.execute("SHOW server_version").fetchone()[0]
        except BaseException:
            self.close()
            raise

    def run(self, name, *args):
        return subprocess.run(
            [str(self.bin / name), *args], check=True, capture_output=True, text=True
        )

    def start(self):
        self.run("pg_ctl", "-D", str(self.data), "-l", str(self.root / "server.log"), "-w", "start")
        self.running = True

    def restart(self):
        # Immediate stop exercises WAL recovery, not only a clean application reopen.
        self.run("pg_ctl", "-D", str(self.data), "-m", "immediate", "-w", "stop")
        self.running = False
        self.start()

    def conninfo(self, database="postgres", user=None):
        return make_conninfo(host=str(self.root), dbname=database, user=user or getpass.getuser())

    def connect(self, database="postgres", user=None):
        return psycopg.connect(self.conninfo(database, user), autocommit=True)

    def database(self):
        self.counter += 1
        database = f"contract_{self.counter}"
        with self.connect() as c:
            c.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE platform_template").format(
                    sql.Identifier(database)
                )
            )
        return database

    def close(self):
        try:
            if self.running:
                self.run("pg_ctl", "-D", str(self.data), "-m", "fast", "-w", "stop")
                self.running = False
        finally:
            self.temp.cleanup()
