"""TLS host for SSO-authenticated rehearsal admission and signed T15 intake."""

import argparse
import asyncio
import importlib
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from rehearsal_runner.approval_handoff import submit_candidate
from rehearsal_runner.attempt_store import AttemptStore
from rehearsal_runner.bucket_planner import build_bucket_host

from .change_intake import Candidate, ChangeIntake
from .executor import _object
from .intake_deployment import ProjectedToken, sender_from_config
from .postgres import PostgresPlanStore
from .registration_sso import SsoRegistrationVerifier
from .rehearsal import RehearsalService
from .rehearsal_sso import RehearsalGrantFile, SsoRehearsalVerifier, create_rehearsal_app
from .runtime_probe import connection_checks
from .store import EventError
from .trusted_rehearsal import AttemptDispatcher
from .window_server import runtime_connection


@dataclass(frozen=True)
class RehearsalPorts:
    catalog: object
    planner: object
    first_run: object = None

    def __post_init__(self):
        if any(not callable(getattr(port, method, None)) for port, method in (
                (self.catalog, "for_plan"), (self.catalog, "for_admission"),
                (self.planner, "binding"), (self.planner, "factory"))):
            raise ValueError("actual_rehearsal_ports_required")
        if self.first_run is not None and not callable(self.first_run):
            raise ValueError("actual_rehearsal_ports_required")


@dataclass(frozen=True)
class RehearsalHost:
    app: object
    intake: object
    service: object
    evidence: object


def build_host(store, *, ports, records, sender, verifier, grants, token_supplier, clock=None):
    if (type(ports) is not RehearsalPorts or type(verifier) is not SsoRehearsalVerifier
            or type(grants) is not RehearsalGrantFile or not callable(token_supplier)
            or getattr(sender, "tenant_id", None) != verifier.identity.tenant_id):
        raise ValueError("actual_rehearsal_host_required")

    def deliver(submission):
        return submit_candidate(submission, candidate_type=Candidate, intake=intake,
                                token_supplier=token_supplier)

    dispatcher = AttemptDispatcher()
    try:
        rehearsals, evidence = build_bucket_host(ports.catalog, ports.planner, records,
                                                 deliver=deliver, dispatch=dispatcher)
        def deny_hold(*_args, **_kwargs):
            raise EventError("forbidden")

        intake = ChangeIntake(store, sender=sender, read_original=evidence.read_original,
                              authorize_hold=deny_hold, clock=clock)
        service = RehearsalService(store, host=rehearsals, check_start=grants.require,
                                   first_run=ports.first_run)
        app = create_rehearsal_app(service, verifier=verifier, access=grants)
    except Exception:
        dispatcher.close()
        raise

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await asyncio.to_thread(dispatcher.close)

    app.router.lifespan_context = lifespan
    return RehearsalHost(app, intake, service, evidence)


def _json(path, limit=65536):
    with Path(path).open() as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("rehearsal_configuration_too_large")
    return json.loads(raw, object_pairs_hook=_object)


def load_host(store, config_file, create_ports):
    config = _json(config_file)
    fields = {"version", "workload_binding", "public_jwks_file", "sso_verifier",
              "grants_file", "records_root", "record_key_file"}
    if (type(config) is not dict or set(config) != fields or type(config["version"]) is not int
            or config["version"] != 1 or not callable(create_ports)):
        raise ValueError("invalid_rehearsal_host_configuration")
    paths = {name: Path(config[name]) for name in ("public_jwks_file", "grants_file", "records_root", "record_key_file")}
    if (any(not p.is_absolute() for p in paths.values()) or not paths["records_root"].is_dir()
            or not paths["grants_file"].is_file()):
        raise ValueError("rehearsal_mounts_required")
    sender = sender_from_config(config["workload_binding"], _json(paths["public_jwks_file"]))
    verifier = SsoRehearsalVerifier(SsoRegistrationVerifier(**config["sso_verifier"]))
    if sender.tenant_id != verifier.identity.tenant_id:
        raise ValueError("same_rehearsal_tenant_required")
    with paths["record_key_file"].open("rb") as stream:
        key = stream.read(4097)
    if not 32 <= len(key) <= 4096:
        raise ValueError("invalid_record_key")
    return build_host(store, ports=create_ports(store), records=AttemptStore(paths["records_root"], key),
        sender=sender, verifier=verifier, grants=RehearsalGrantFile(paths["grants_file"]), token_supplier=ProjectedToken())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--ports", required=True, help="reviewed module:factory(store) returning RehearsalPorts")
    parser.add_argument("--tls-cert", required=True)
    parser.add_argument("--tls-key", required=True)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", args.ports):
            raise ValueError()
        if not all(Path(p).is_file() for p in (args.config, args.tls_cert, args.tls_key)):
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
    except Exception:
        raise SystemExit("rehearsal_startup_unavailable") from None
    uvicorn.run(host.app, host="0.0.0.0", port=8443, workers=1,
        ssl_certfile=args.tls_cert, ssl_keyfile=args.tls_key, access_log=False,
        proxy_headers=False, server_header=False, limit_concurrency=16, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
