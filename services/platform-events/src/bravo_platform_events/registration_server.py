"""Operator-only host. Catalog, reader credentials and human grants are explicit mounts."""

import argparse
import json
from pathlib import Path

import uvicorn

from .operator_identity import OperatorTokenVerifier
from .postgres import PostgresPlanStore
from .registration import Registry
from .registration_api import create_registration_app
from .registration_probe import EnvironmentRef, SourceRelease
from .registration_sso import SsoRegistrationVerifier
from .runtime_probe import connection_checks
from .window_server import runtime_connection


def build_registry(store, config, grants_file):
    """The reviewed catalog and live grants file → Registry. Shared by the SOVA MCP host."""
    environments = {}
    for row in config["environments"]:
        row = row.copy()
        ref = row.pop("connection_ref")
        row["ca_file"] = Path(row["ca_file"])
        row["read_token"] = Path(row.pop("token_file")).read_text
        env = EnvironmentRef(**row)
        key = (env.tenant_id, ref)
        if key in environments:
            raise ValueError("duplicate_connection_ref")
        environments[key] = env
    releases = [SourceRelease(**(row | {"checkout": Path(row["checkout"])})) for row in config["releases"]]

    def can_register(actor):
        # Grants are tenant-wide (no plan exists yet). Read the current projected
        # file on each check, including the check after the DB lock is acquired.
        try:
            grants = json.loads(grants_file.read_text())
            return any((g["tenant_id"], g["subject_id"]) == (actor.tenant_id, actor.subject_id) for g in grants)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    return Registry(store, environments=environments, releases=releases, can_register=can_register,
                    evaluation_profiles=config.get("evaluation_profiles", ()))


def build_app(store, config_file, grants_file):
    config = json.loads(config_file.read_text())
    mode = config.get("identity_mode", "operator_jwt")
    if mode == "aws_sso" and "operator_verifier" not in config:
        identity = SsoRegistrationVerifier(**config["sso_verifier"])
    elif mode == "operator_jwt" and "sso_verifier" not in config:
        identity = OperatorTokenVerifier(**config["operator_verifier"])
    else:
        raise ValueError("invalid_registration_identity_mode")
    app = create_registration_app(build_registry(store, config, grants_file), verify_identity=identity)
    if config.get("gate_records") is not None:
        from .gate_record_host import (
            AUDIENCE,
            GateRecordGrants,
            GateRecordHost,
            GateRecordVerifier,
            create_gate_record_app,
        )
        gate = config["gate_records"]
        if (mode != "aws_sso" or type(gate) is not dict
                or gate != {"grants_file": "/registration/gate-grants.json"}):
            raise ValueError("explicit_gate_record_configuration_required")
        verifier = GateRecordVerifier(SsoRegistrationVerifier(**(config["sso_verifier"] | {"audience": AUDIENCE})))
        host = GateRecordHost(store, GateRecordGrants(gate["grants_file"]))
        app.mount("/gate-records", create_gate_record_app(host, verifier))
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args = parser.parse_args(argv)
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("provide both --tls-cert and --tls-key")
    try:
        connection = runtime_connection()
        connection_checks(connection)
        store = PostgresPlanStore(connection)
        with store._transaction() as c:
            if c.execute("SELECT version FROM schema_version").fetchall() != [{"version": 3}]:
                raise ValueError("registration_schema_required")
        app = build_app(store, Path("/registration/catalog.json"), Path("/registration/grants.json"))
    except Exception as error:
        from .startup import reason

        raise SystemExit("registration_startup_unavailable:" + reason(error)) from None
    tls = ({"ssl_certfile": str(args.tls_cert), "ssl_keyfile": str(args.tls_key)}
           if args.tls_cert else {})
    uvicorn.run(app, host="0.0.0.0", port=8443 if tls else 8080, workers=1,
                **tls, access_log=False, proxy_headers=False,
                server_header=False, limit_concurrency=16, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
