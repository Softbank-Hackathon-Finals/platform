"""Production HTTP entry point; runtime DB and producer identity come from mounts."""

import json
from pathlib import Path

import uvicorn
from psycopg.conninfo import make_conninfo

from .postgres import PostgresPlanStore
from .runtime_probe import connection_checks
from .window_api import FileProducerCredential, create_window_app
from .window_store import WindowIntake


def runtime_connection():
    return make_conninfo(
        host="platform-db-rw.platform-state.svc", port=5432, dbname="platform",
        user=Path("/credential/username").read_text(),
        password=Path("/credential/password").read_text(),
        sslmode="verify-full", sslrootcert="/ca/ca.crt", connect_timeout=5,
    )


def main():
    try:
        conninfo = runtime_connection()
        connection_checks(conninfo)
        store = PostgresPlanStore(conninfo)
        with store._transaction() as c:
            if c.execute("SELECT version FROM schema_version").fetchall() != [{"version": 2}]:
                raise ValueError("window_schema_required")
        app = create_window_app(WindowIntake(store),
            resolve_producer=FileProducerCredential(Path("/producer/producer.json")))
    except Exception:
        print(json.dumps({"status": "FAILED", "code": "window_startup_unavailable"}), flush=True)
        raise SystemExit(1) from None
    # No request paths, headers, credentials or raw driver errors in access logs.
    uvicorn.run(app, host="0.0.0.0", port=8080, workers=1, access_log=False,
                proxy_headers=False, server_header=False, limit_concurrency=16,
                timeout_keep_alive=5)


if __name__ == "__main__":
    main()
