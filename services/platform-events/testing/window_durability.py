"""Synthetic crash-recovery and restricted-runtime check for window_cloud_host."""

import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from bravo_platform_events.plans import PlanSpec
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.window_store import ProducerIdentity, WindowIntake


def main():
    socket = os.environ["BRAVO_TEST_SOCKET"]
    if not socket.startswith("/tmp/bravo-t10-window-"):
        raise ValueError("isolated_socket_required")
    db = "window_durability"
    sql = Path(__file__).parents[1] / "sql"
    connect = lambda database, user: psycopg.connect(  # noqa: E731
        make_conninfo(host=socket, dbname=database, user=user), autocommit=True)
    if sys.argv[1] == "prepare":
        with connect("postgres", "postgres") as c:
            c.execute("CREATE DATABASE window_durability TEMPLATE platform_template")
        with connect(db, "fixture_migration") as c:
            c.execute((sql / "0002_sli_windows.sql").read_text())
    store = PostgresPlanStore(make_conninfo(host=socket, dbname=db, user="fixture_runtime"))
    now = "2026-11-08T05:31:21Z"
    intake = WindowIntake(store, clock=lambda: now)
    producer = ProducerIdentity("cloud-producer", "cloud-fixture", "plan-demo", "fixture-key-v1", "2026-11-08T06:00:00Z")
    document = json.loads((Path(__file__).parents[3] / "docs/observability/examples/window-shift-10.json").read_text())
    if sys.argv[1] == "prepare":
        store.register_plan(PlanSpec("plan-demo", "cloud-fixture", "fixture-workload", "source", "target", "a" * 64, "b" * 64),
                            run_id="cloud-fixture", occurred_at=now)
        intake.set_binding(producer, enabled=True, expected_revision=0, administration_ref="fixture-admin", command_key="fixture-grant")
        intake.receive(producer, document)
        return
    with store._transaction() as c:
        assert intake.latest_in_transaction(c, "plan-demo") == document
    denied = 0
    with connect(db, "fixture_runtime") as c:
        version = c.execute("SHOW server_version").fetchone()[0]
        for command in ("ALTER TABLE bravo_platform.sli_windows ADD COLUMN forbidden INT",
                        "UPDATE bravo_platform.sli_windows SET document_json='{}'",
                        "DELETE FROM bravo_platform.plan_commands"):
            try:
                c.execute(command)
            except psycopg.errors.InsufficientPrivilege:
                denied += 1
        assert denied == 3
    print(json.dumps(dict(preserved_window=True, postgres_version=version, runtime_privilege_denials=denied)))


if __name__ == "__main__":
    main()
