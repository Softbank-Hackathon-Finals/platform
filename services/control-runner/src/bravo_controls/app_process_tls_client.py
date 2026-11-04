"""Fixed SELECT 1 child. Invoked with -I and a fresh, bounded environment only."""

import json
import os


def main():
    import psycopg

    config = json.loads(os.environ["BRAVO_TLS_CONFIG"])
    if (psycopg.__version__ != config["psycopg_version"]
            or psycopg.pq.version() != config["libpq_version"]):
        return {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}
    # All destinations/options come from the server registration. No caller SQL.
    with psycopg.connect(
        host=config["hostname"], hostaddr=config["address"], port=config["port"],
        dbname=config["database"], user=config["username"], password=os.environ["PGPASSWORD"],
        sslmode="verify-full", sslrootcert=config["ca_file"],
        ssl_min_protocol_version="TLSv1.2", gssencmode="disable",
        require_auth="scram-sha-256", connect_timeout=5,
        application_name="bravo-app-tls-probe",
        options="-c default_transaction_read_only=on -c statement_timeout=5000",
    ) as conn:
        row = conn.execute("SELECT 1").fetchone()
        if row != (1,) or not conn.pgconn.ssl_in_use:
            return {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}
    return {"outcome": "allow", "reason": None}


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        # Connection errors may contain credential/host/server controlled text.
        result = {"outcome": None, "reason": "TOOL_ERROR"}
    print(json.dumps(result, sort_keys=True))
