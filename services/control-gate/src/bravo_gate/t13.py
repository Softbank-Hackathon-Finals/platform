"""Read-only T13 schema capture for T19's registered readiness policy.

This is not a credential provider, approval, or cutover implementation. The host
binds the connection factory to a registered DB and authenticates/stores captures.
Sequence correction receipts and replication observations remain separate inputs;
this module never invents those observations or runs setval on a live database.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta

from .contract import GateError, canonical, fingerprint, server_time
from .models import GateContext, RunSnapshot, identity
from .readiness import ReadinessBinding, ReadinessPolicy


@dataclass(frozen=True)
class DatabaseRegistration:
    environment_id: str
    environment_ref: str
    database_ref: str
    database_name: str
    role_name: str

    def __post_init__(self):
        for value in (self.environment_id, self.environment_ref, self.database_ref,
                      self.database_name, self.role_name):
            identity(value)


@dataclass(frozen=True)
class SchemaCapture:
    context_digest: str
    policy_digest: str
    registration: DatabaseRegistration
    document_json: bytes
    errors: tuple[tuple[str, str], ...]

    @property
    def document(self):
        from .contract import decode
        return decode(self.document_json)


def _verified_connection(connection, registration):
    from psycopg import Connection
    from psycopg.pq import TransactionStatus

    if (not isinstance(connection, Connection) or connection.closed
            or not connection.autocommit
            or connection.info.transaction_status != TransactionStatus.IDLE
            or connection.info.get_parameters().get("sslmode") != "verify-full"
            or not connection.pgconn.ssl_in_use):
        raise GateError("READINESS_VERIFIED_CONNECTION_REQUIRED")
    # The factory owns host/CA/cluster UID binding. Check actual DB/role as well,
    # rather than trusting the username/database string in a DSN.
    with connection.transaction():
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '3s'")
        connection.execute("SET LOCAL lock_timeout = '1s'")
        # A database/role default can place an application schema before pg_catalog.
        # Do not execute its functions or operators while establishing DB identity.
        connection.execute("SET LOCAL search_path = pg_catalog")
        row = connection.execute(
            "SELECT pg_catalog.current_database(), session_user, current_user, rolsuper, rolbypassrls "
            "FROM pg_catalog.pg_roles WHERE rolname = current_user"
        ).fetchone()
    if row != (registration.database_name, registration.role_name, registration.role_name, False, False):
        raise GateError("READINESS_DATABASE_IDENTITY_MISMATCH")


def collect_schema(
    ctx: GateContext, policy: ReadinessPolicy, registration: DatabaseRegistration, *,
    side: str, connect: Callable[[DatabaseRegistration], AbstractContextManager],
    clock: Callable[[], datetime],
) -> SchemaCapture:
    """Read actual schema/PK via the T13 implementation; failure remains unknown.

Dedicated idle TLS connection, non-superuser/non-BYPASSRLS role, fixed server
policy only. A second read detects schema changes during collection. This does
not lock DDL across gate approval: T12 must recheck after fencing at cutover.
"""
    from bravo_cutover.db_fingerprint import HASH_RULE, schema_fingerprint

    if side not in ("source", "target"):
        raise GateError("INVALID_READINESS_SIDE")
    if (registration.environment_id != getattr(ctx, side + "_environment_id")
            or registration.environment_ref != getattr(ctx, side + "_environment_ref")):
        raise GateError("READINESS_RESOURCE_MISMATCH")
    if policy.schema_rule != HASH_RULE:
        raise GateError("READINESS_T13_RULE_MISMATCH")
    started = server_time(clock)
    expires = started + timedelta(seconds=min(30, policy.max_age_seconds))
    rows = {name: {"name": name, "schema_hash": None, "primary_key": None} for name in policy.tables}
    errors = {}

    def within_deadline():
        now = server_time(clock)
        if not started <= now < expires:
            raise GateError("READINESS_COLLECTION_EXPIRED")

    try:
        with connect(registration) as connection:
            _verified_connection(connection, registration)
            for name in policy.tables:
                within_deadline()
                try:
                    observed = schema_fingerprint(connection, name)
                    rows[name].update(schema_hash=observed["schema_hash"], primary_key=observed["primary_key"])
                except Exception:
                    # T13's fixed error detail stays in its private diagnostic;
                    # neither SDK exceptions nor SQL/DSN text leave this boundary.
                    errors[name] = "SCHEMA_UNCONFIRMED"
            for name in policy.tables:
                within_deadline()
                if name in errors:
                    continue
                try:
                    reread = schema_fingerprint(connection, name)
                    if any(reread[k] != rows[name][k] for k in ("schema_hash", "primary_key")):
                        raise GateError("SCHEMA_CHANGED")
                except Exception:
                    rows[name].update(schema_hash=None, primary_key=None)
                    errors[name] = "SCHEMA_CHANGED_OR_UNCONFIRMED"
            _verified_connection(connection, registration)
            within_deadline()
    except Exception:
        # Connection, identity, deadline or final identity failure invalidates
        # the whole capture; never return the earlier partial success.
        for name in policy.tables:
            rows[name].update(schema_hash=None, primary_key=None)
            errors[name] = "SCHEMA_CAPTURE_UNAVAILABLE"
    doc = dict(environment_id=registration.environment_id, environment_ref=registration.environment_ref,
               database_ref=registration.database_ref, schema_rule=policy.schema_rule,
               observed_at=started.isoformat(), expires_at=expires.isoformat(),
               tables=list(rows.values()))
    return SchemaCapture(ctx.fingerprint, policy.fingerprint, registration, canonical(doc), tuple(sorted(errors.items())))


def assemble_snapshot(
    ctx: GateContext, policy: ReadinessPolicy, source: SchemaCapture, target: SchemaCapture, *,
    snapshot_ref: str, replication: dict, sequences: list[dict],
) -> tuple[ReadinessBinding, RunSnapshot]:
    """Package host-authenticated observations; does not authenticate JSON input.

The host must authenticate provenance/receipt scope for replication and sequence
rehearsal records before calling. Missing records must remain missing/unknown;
never substitute estimates, requested setval values, or a UI PASS flag.
The resulting snapshot still requires the existing T19 snapshot authority.
"""
    for side, capture in (("source", source), ("target", target)):
        if not isinstance(capture, SchemaCapture) or capture.context_digest != ctx.fingerprint or capture.policy_digest != policy.fingerprint:
            raise GateError("READINESS_CAPTURE_BINDING_MISMATCH")
        reg = capture.registration
        if (reg.environment_id != getattr(ctx, side + "_environment_id")
                or reg.environment_ref != getattr(ctx, side + "_environment_ref")):
            raise GateError("READINESS_RESOURCE_MISMATCH")
    doc = dict(kind="data-readiness.v1", context_digest=ctx.fingerprint, policy_digest=policy.fingerprint,
               source=source.document, target=target.document, replication=replication, sequences=sequences)
    snapshot = RunSnapshot(snapshot_ref, canonical(doc))
    binding = ReadinessBinding(ctx.fingerprint, policy, source.registration.database_ref,
                               target.registration.database_ref, snapshot_ref, fingerprint(doc))
    return binding, snapshot
