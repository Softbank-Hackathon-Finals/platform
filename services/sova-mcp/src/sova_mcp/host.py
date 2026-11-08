"""SOVA MCP host on the platform RKE2: same DB, catalog and grants as the T10 registration host.

Mounts (reviewed, read-only): /sova/config.json, the registration catalog/grants and TLS.
Identity is the team's AWS SSO STS proof for the distinct `sova-mcp` audience, pinned per
person, so a registration, operator or console proof is never an MCP credential (and back).
"""

import argparse
import json
from pathlib import Path

import uvicorn

AUDIENCE = "sova-mcp"
CONFIG_FIELDS = {"resource_url", "allowed_hosts", "console_url", "identities", "environments",
                 "observability", "security"}


def load(config_path: Path, store, catalog: Path, grants: Path):
    from bravo_platform_events.registration_server import build_registry
    from bravo_platform_events.registration_sso import SsoRegistrationVerifier

    from .backend import SovaBackend, Upstream
    from .server import create_app

    config = json.loads(config_path.read_text())
    if type(config) is not dict or set(config) != CONFIG_FIELDS:
        raise ValueError("invalid_sova_mcp_configuration")
    verifiers = [SsoRegistrationVerifier(**(row | {"audience": AUDIENCE})) for row in config["identities"]]
    if not verifiers or len({v.subject_id for v in verifiers}) != len(verifiers):
        raise ValueError("sova_mcp_identities_required")

    def verify(request):
        # ponytail: one STS call per pinned person until a proof matches; TokenCache makes it once per proof.
        return next((a for a in (v(request) for v in verifiers) if a is not None), None)

    def upstream(row):
        if not row:
            return None
        return Upstream(row["base_url"], Path(row["token_file"]), ca_file=Path(row["ca_file"]) if row.get("ca_file") else None)

    backend = SovaBackend(store, build_registry(store, json.loads(catalog.read_text()), grants),
                          config["console_url"], config["environments"],
                          upstream(config["observability"]), upstream(config["security"]))
    return create_app(backend, verify, resource_url=config["resource_url"], allowed_hosts=config["allowed_hosts"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/sova/config.json"))
    parser.add_argument("--catalog", type=Path, default=Path("/registration/catalog.json"))
    parser.add_argument("--grants", type=Path, default=Path("/registration/grants.json"))
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args = parser.parse_args(argv)
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("provide both --tls-cert and --tls-key")
    try:
        from bravo_platform_events.postgres import PostgresPlanStore
        from bravo_platform_events.runtime_probe import connection_checks
        from bravo_platform_events.window_server import runtime_connection

        connection = runtime_connection()
        connection_checks(connection)
        store = PostgresPlanStore(connection)
        with store._transaction() as c:
            if c.execute("SELECT version FROM schema_version").fetchall() != [{"version": 3}]:
                raise ValueError("registration_schema_required")
        app = load(args.config, store, args.catalog, args.grants)
    except Exception as error:
        from bravo_platform_events.startup import reason

        raise SystemExit("sova_mcp_startup_unavailable:" + reason(error)) from None
    # Without TLS the listener is plain HTTP for the tailnet proxy only (WireGuard to the client), like window intake.
    tls = {"ssl_certfile": str(args.tls_cert), "ssl_keyfile": str(args.tls_key)} if args.tls_cert else {}
    uvicorn.run(app, host="0.0.0.0", port=8443 if tls else 8080, workers=1, **tls, access_log=False,
                proxy_headers=False, server_header=False, limit_concurrency=128, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
