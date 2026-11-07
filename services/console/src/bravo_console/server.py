"""Optional console entry point for the existing approval host and protected mounts."""

import argparse
import hashlib
import importlib.util
import json
import re
from pathlib import Path

RUNTIME_SOURCE = Path("/app/runtime-source.json")
CUSTOMER_PRODUCER = Path("/app/infra/iac/customer_status.py")


def customer_producer():
    metadata = json.loads(RUNTIME_SOURCE.read_text())
    if (
        type(metadata) is not dict
        or set(metadata) != {"source_revision", "customer_status_sha256"}
        or not isinstance(metadata["source_revision"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", metadata["source_revision"])
        or not isinstance(metadata["customer_status_sha256"], str)
        or hashlib.sha256(CUSTOMER_PRODUCER.read_bytes()).hexdigest()
        != metadata["customer_status_sha256"]
    ):
        raise ValueError("customer_producer_source_mismatch")
    spec = importlib.util.spec_from_file_location("sova_customer_status", CUSTOMER_PRODUCER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, metadata["source_revision"]


def configured(store, host_config, console_config):
    from bravo_platform_events.approval_server import load_host
    from bravo_platform_events.operator_identity import OPERATOR_SSO_AUDIENCE, OperatorSsoVerifier
    from bravo_platform_events.operator_ports import create
    from bravo_platform_events.registration_sso import SsoRegistrationVerifier

    from .runtime import NativeCustomerSource, assemble

    config = json.loads(Path(console_config).read_text())
    original = json.loads(Path(host_config).read_text())
    people = original["operator_verifier"]
    people = people if isinstance(people, list) else [people]
    verifier = OperatorSsoVerifier(
        [
            SsoRegistrationVerifier(**(person | {"audience": OPERATOR_SSO_AUDIENCE}))
            for person in people
        ]
    )
    module, producer_revision = customer_producer()
    source = NativeCustomerSource(
        config["customers"],
        inspect_customer=module.inspect,
        reader=module.reader,
        producer_revision=producer_revision,
    )
    host = load_host(store, host_config, create)
    return assemble(host, store, verifier, config, source)


def main(argv=None):
    import uvicorn
    from bravo_platform_events.postgres import PostgresPlanStore
    from bravo_platform_events.runtime_probe import connection_checks
    from bravo_platform_events.window_server import runtime_connection

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--console-config", required=True)
    parser.add_argument("--tls-cert", required=True)
    parser.add_argument("--tls-key", required=True)
    args = parser.parse_args(argv)
    try:
        connection = runtime_connection()
        connection_checks(connection)
        store = PostgresPlanStore(connection)
        with store._transaction() as c:
            if c.execute("SELECT version FROM schema_version").fetchall() != [{"version": 3}]:
                raise ValueError("existing_schema_3_required")
        app = configured(store, args.config, args.console_config)
    except Exception:
        raise SystemExit("console_runtime_configuration_unavailable") from None
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8443,
        workers=1,
        ssl_certfile=args.tls_cert,
        ssl_keyfile=args.tls_key,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        limit_concurrency=16,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
