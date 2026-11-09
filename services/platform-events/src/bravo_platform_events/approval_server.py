"""Explicit TLS entry point for a reviewed, single-process T10/T15 host."""

import argparse
import importlib
import json
import re
from pathlib import Path

import uvicorn
from rehearsal_runner.attempt_store import AttemptStore

from .intake_deployment import ProjectedToken, sender_from_config
from .operator_identity import OperatorTokenVerifier
from .postgres import PostgresPlanStore
from .runtime_probe import connection_checks
from .trusted_rehearsal import build_trusted_host
from .window_server import runtime_connection

_FIELDS = {"version", "workload_binding", "public_jwks_file", "operator_verifier",
           "records_root", "record_key_file"}


def load_host(store, config_file, create_ports):
    """Only deployment-owned files/code. No token/body can select paths or ports."""
    config = json.loads(Path(config_file).read_text())
    if (type(config) is not dict or set(config) != _FIELDS
            or type(config["version"]) is not int or config["version"] != 1
            or not callable(create_ports)):
        raise ValueError("invalid_approval_host_configuration")
    paths = {key: Path(config[key]) for key in ("public_jwks_file", "records_root", "record_key_file")}
    if any(not path.is_absolute() for path in paths.values()) or not paths["records_root"].is_dir():
        raise ValueError("approval_host_mounts_required")
    sender = sender_from_config(config["workload_binding"], json.loads(paths["public_jwks_file"].read_text()))
    operator = config["operator_verifier"]
    people = operator if isinstance(operator, list) else [operator]
    if people and all(isinstance(o, dict) and "expected_sso_role_arn" in o for o in people):
        # AWS IAM Identity Center proof (same as registration), signed for the operator audience.
        from .operator_identity import OPERATOR_SSO_AUDIENCE, OperatorSsoVerifier
        from .registration_sso import SsoRegistrationVerifier

        verifier = OperatorSsoVerifier([SsoRegistrationVerifier(**(o | {"audience": OPERATOR_SSO_AUDIENCE}))
                                        for o in people])
    else:
        verifier = OperatorTokenVerifier(**operator)
    with paths["record_key_file"].open("rb") as stream:
        key = stream.read(4097)
    if not 32 <= len(key) <= 4096:
        raise ValueError("invalid_record_key")
    records = AttemptStore(paths["records_root"], key)
    return build_trusted_host(store, ports=create_ports(store), records=records,
        sender=sender, verifier=verifier, token_supplier=ProjectedToken())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--ports", required=True, help="reviewed deployment module:factory(store)")
    parser.add_argument("--tls-cert", required=True)
    parser.add_argument("--tls-key", required=True)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", args.ports):
            raise ValueError()
        if not all(Path(path).is_file() for path in (args.tls_cert, args.tls_key)):
            raise ValueError()
        module, factory = args.ports.split(":")
        create_ports = getattr(importlib.import_module(module), factory)
        connection = runtime_connection()
        connection_checks(connection)
        store = PostgresPlanStore(connection)
        with store._transaction() as c:
            if c.execute("SELECT version FROM schema_version").fetchall() != [{"version": 3}]:
                raise ValueError()
        host = load_host(store, args.config, create_ports)
    except Exception as error:
        from .startup import reason

        raise SystemExit("approval_startup_unavailable:" + reason(error)) from None
    uvicorn.run(host.app, host="0.0.0.0", port=8443, workers=1,
        ssl_certfile=args.tls_cert, ssl_keyfile=args.tls_key, access_log=False,
        proxy_headers=False, server_header=False, limit_concurrency=16, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
