"""ENC-01.5: live Pooler configuration AND the actual upstream DB connection.

The registered collector is trusted code, never an uploaded report. It must read
SHOW CONFIG before/after a query through one pinned Pooler Pod, and read
pg_stat_ssl/pg_stat_activity for that query's pg_backend_pid(). A file on disk or
client-to-Pooler TLS alone is insufficient. No mutation/restart is performed here.
"""

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .runner import Environment, Observation, RunContext, TestSpec

SPEC = TestSpec("ENC-01.5", "ENC-01", "1", "enabled", "not_applicable")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}\Z")
_SHA = re.compile(r"sha256:[a-f0-9]{64}\Z")
_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware observation time required")
    return value.astimezone(UTC)


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class BackendTarget:
    app: str
    environment: Environment
    pooler_uid: str
    pooler_pod_uid: str
    database_uid: str
    database: str
    pooler_address: str
    database_address: str
    registration_sha256: str

    def __post_init__(self):
        if not isinstance(self.environment, Environment) or not all(
            isinstance(x, str) and _NAME.fullmatch(x)
            for x in (self.app, self.pooler_uid, self.pooler_pod_uid, self.database_uid, self.database)
        ):
            raise ValueError("registered application and resource identities required")
        for value in (self.pooler_address, self.database_address):
            if not isinstance(value, str):
                raise ValueError("literal registered address required")
            ipaddress.ip_address(value)
        if not isinstance(self.registration_sha256, str) or not _SHA.fullmatch(self.registration_sha256):
            raise ValueError("registration digest required")

    def binding(self):
        return {key: getattr(self, key) for key in (
            "pooler_uid", "pooler_pod_uid", "database_uid", "database",
            "pooler_address", "database_address", "registration_sha256",
        )}


def assess(target, context, report, started, finished):
    """Return only verdict facts; never persist raw SQL/tool diagnostics."""
    try:
        if not isinstance(report, dict) or set(report) != {
            "run_id", "before", "after", "connection",
        } or report["run_id"] != context.run_id:
            raise ValueError("wrong invocation")
        before, after, connection = (report[x] for x in ("before", "after", "connection"))
        for reading in (before, after):
            if not isinstance(reading, dict) or set(reading) != {"binding", "collected_at", "server_tls_sslmode"}:
                raise ValueError("invalid runtime readback")
            if reading["binding"] != target.binding() or reading["server_tls_sslmode"] not in _MODES:
                raise ValueError("resource changed or unknown runtime mode")
        if not isinstance(connection, dict) or set(connection) != {
            "collected_at", "backend_started_at", "database", "client_address",
            "server_address", "ssl", "protocol", "pid",
        }:
            raise ValueError("invalid database readback")
        times = [started, _utc(before["collected_at"]), _utc(connection["collected_at"]),
                 _utc(after["collected_at"]), finished]
        if times != sorted(times) or finished - started > timedelta(seconds=30):
            raise ValueError("stale or out of order readback")
        if before["server_tls_sslmode"] != after["server_tls_sslmode"]:
            raise ValueError("configuration changed during query")
        if (connection["database"] != target.database
                or ipaddress.ip_address(connection["client_address"]) != ipaddress.ip_address(target.pooler_address)
                or ipaddress.ip_address(connection["server_address"]) != ipaddress.ip_address(target.database_address)
                or type(connection["pid"]) is not int or connection["pid"] <= 0
                or type(connection["ssl"]) is not bool):
            raise ValueError("wrong upstream database connection")
        # A pre-existing pooled connection cannot demonstrate the currently
        # loaded certificate-validation policy. Collect without disrupting apps;
        # if no fresh backend can be obtained, leave UNKNOWN instead of restarting.
        if not times[1] <= _utc(connection["backend_started_at"]) <= times[2]:
            raise ValueError("old or future pooled connection")
        if connection["ssl"] and connection["protocol"] not in {"TLSv1.2", "TLSv1.3"}:
            if connection["protocol"] not in {"TLSv1", "TLSv1.1"}:
                raise ValueError("unknown TLS protocol")
        elif not connection["ssl"] and connection["protocol"] is not None:
            raise ValueError("inconsistent TLS readback")
        secure = (before["server_tls_sslmode"] == "verify-full" and connection["ssl"]
                  and connection["protocol"] in {"TLSv1.2", "TLSv1.3"})
        return "enabled" if secure else "disabled", None, {
            "runtime_mode": before["server_tls_sslmode"], "upstream_ssl": connection["ssl"],
            "protocol": connection["protocol"], "fresh_backend": True, "binding_unchanged": True,
        }
    except (ValueError, TypeError, KeyError, OverflowError):
        return None, "UNEXPECTED_RESPONSE", {"readback_valid": False}


class PoolerBackendTLSProbe:
    def __init__(self, target, *, collector, evidence_sink, clock=lambda: datetime.now(UTC)):
        if not isinstance(target, BackendTarget) or not all(callable(x) for x in (collector, evidence_sink, clock)):
            raise ValueError("registered target, collector and durable evidence sink required")
        self.target, self.collector, self.sink, self.clock = target, collector, evidence_sink, clock

    def __call__(self, context: RunContext, spec: TestSpec) -> Observation:
        if (not isinstance(context, RunContext) or spec != SPEC
                or context.app != self.target.app or context.environment != self.target.environment
                or not isinstance(context.run_id, str) or not _NAME.fullmatch(context.run_id)):
            raise ValueError("collector scope or method mismatch")
        started = _utc(self.clock())
        try:
            report = self.collector(context, self.target, started + timedelta(seconds=30))
            finished = _utc(self.clock())
            observed, reason, facts = assess(self.target, context, report, started, finished)
        except (TimeoutError, PermissionError, OSError, ValueError) as error:
            finished = _utc(self.clock())
            observed = None
            reason = ("TIMEOUT" if isinstance(error, TimeoutError) else "OBSERVATION_DENIED"
                      if isinstance(error, PermissionError) else "TOOL_ERROR")
            facts = {"readback_valid": False}
        payload = _bytes({
            "format_version": "1", "test_id": SPEC.test_id, "method_version": SPEC.method_version,
            "run_id": context.run_id, "app": context.app,
            "environment": {"id": context.environment.id, "role": context.environment.role, "ref": context.environment.ref},
            "binding_sha256": _sha(_bytes(self.target.binding())),
            "started_at": started.isoformat(), "collected_at": finished.isoformat(),
            "observed": observed, "unknown_reason": reason, "facts": facts,
        })
        self.sink(payload)
        return Observation(context, SPEC.test_id, "1", observed, "not_applicable", finished,
                           "bravo-pooler-backend-tls", "1", _sha(payload), reason)
