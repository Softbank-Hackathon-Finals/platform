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
BACKEND_SQL = """
SELECT pg_catalog.current_database(), pg_catalog.host(pg_catalog.inet_client_addr()),
       pg_catalog.host(pg_catalog.inet_server_addr()), s.ssl, s.version,
       pg_catalog.pg_backend_pid(), a.backend_start, CURRENT_USER, SESSION_USER,
       pg_catalog.current_setting('transaction_read_only')
FROM pg_catalog.pg_stat_ssl AS s
JOIN pg_catalog.pg_stat_activity AS a ON a.pid = s.pid
WHERE s.pid = pg_catalog.pg_backend_pid()
LIMIT 2
"""


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


class PostgreSQLPoolerBackendReader:
    """Fixed read-only queries through one directly pinned Pooler listener.

The host authenticates registration, owns an idle stats-user connection to
`pgbouncer`, and creates a NEW dedicated app connection on demand. Credentials
stay at that host boundary. No PAUSE, RECONNECT, reload, termination or DDL is
used to force a fresh backend; an existing backend remains UNKNOWN.

The host MUST supervise the full call deadline in its worker process (including
SHOW CONFIG and connection creation). Query timeouts alone cannot interrupt a
stalled PgBouncer control connection. Shared app/control connections are invalid.
"""

    def __init__(self, stats_connection, *, open_application_connection,
                 application_user, stats_user, pooler_hostname, pooler_port,
                 read_registration, clock=lambda: datetime.now(UTC)):
        if (not all(callable(x) for x in (open_application_connection, read_registration, clock))
                or any(not isinstance(x, str) or not _NAME.fullmatch(x)
                       for x in (application_user, stats_user, pooler_hostname))
                or application_user == stats_user or type(pooler_port) is not int
                or not 1 <= pooler_port <= 65535):
            raise ValueError("registered reader identities and listener required")
        self.stats = stats_connection
        self.open_application = open_application_connection
        self.application_user, self.stats_user = application_user, stats_user
        self.hostname, self.port = pooler_hostname, pooler_port
        self.read_registration, self.clock = read_registration, clock

    def _validate_connection(self, connection, target, database, username):
        from psycopg import Connection
        from psycopg.pq import TransactionStatus

        if (not isinstance(connection, Connection) or connection.closed
                or connection.info.transaction_status != TransactionStatus.IDLE):
            raise ValueError("dedicated idle reader required")
        parameters = connection.info.get_parameters()
        if (parameters.get("sslmode") != "verify-full" or not connection.pgconn.ssl_in_use
                or not parameters.get("sslrootcert")
                or parameters.get("host") != self.hostname
                or parameters.get("hostaddr") != target.pooler_address
                or connection.info.port != self.port
                or connection.info.dbname != database or connection.info.user != username):
            raise ValueError("registered verified listener and identity required")

    def __call__(self, context, target, deadline):
        from psycopg import Error
        from psycopg.errors import InsufficientPrivilege, QueryCanceled

        if (not isinstance(target, BackendTarget) or not isinstance(context, RunContext)
                or context.app != target.app or context.environment != target.environment):
            raise ValueError("registered pooler invocation required")

        def check():
            if _utc(self.clock()) >= _utc(deadline):
                raise TimeoutError("pooler read budget exceeded")

        def registration():
            check()
            if self.read_registration(context, target) != target.binding():
                raise ValueError("pooler registration changed")
            check()

        def configuration():
            check()
            self._validate_connection(self.stats, target, "pgbouncer", self.stats_user)
            if not self.stats.autocommit:
                raise ValueError("pooler stats connection must use autocommit")
            with self.stats.cursor() as cursor:
                cursor.execute("SHOW CONFIG")
                rows = cursor.fetchmany(129)
            if (not rows or len(rows) > 128
                    or any(not isinstance(row, tuple) or len(row) < 2 for row in rows)):
                raise ValueError("complete runtime configuration required")
            modes = [row[1] for row in rows if row[0] == "server_tls_sslmode"]
            if len(modes) != 1 or modes[0] not in _MODES:
                raise ValueError("runtime TLS mode unconfirmed")
            check()
            return {"binding": target.binding(), "collected_at": _utc(self.clock()),
                    "server_tls_sslmode": modes[0]}

        application = None
        try:
            registration()
            before = configuration()
            check()
            application = self.open_application()
            if application is self.stats:
                raise ValueError("separate stats and application connections required")
            self._validate_connection(application, target, target.database, self.application_user)
            with application.transaction():
                with application.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute("SET LOCAL statement_timeout = '3s'")
                    cursor.execute("SET LOCAL lock_timeout = '1s'")
                    cursor.execute("SET LOCAL TIME ZONE 'UTC'")
                    cursor.execute(BACKEND_SQL)
                    rows = cursor.fetchmany(2)
                    if (len(rows) != 1 or len(rows[0]) != 10
                            or rows[0][7:10] != (self.application_user, self.application_user, "on")):
                        raise ValueError("one authenticated read-only backend required")
                    row = rows[0]
                    connection = {"collected_at": _utc(self.clock()), "backend_started_at": row[6],
                                  "database": row[0], "client_address": row[1], "server_address": row[2],
                                  "ssl": row[3], "protocol": row[4], "pid": row[5]}
                    check()
                    # Keep the read-only transaction pinned until the second
                    # configuration read. Never issue an application write.
                    after = configuration()
            registration()
            return {"run_id": context.run_id, "before": before, "after": after,
                    "connection": connection}
        except InsufficientPrivilege:
            raise PermissionError("pooler metadata read denied") from None
        except QueryCanceled:
            raise TimeoutError("pooler metadata query timed out") from None
        except Error:
            raise OSError("pooler metadata transport unavailable") from None
        finally:
            if application is not None and application is not self.stats:
                # A factory must return a dedicated psycopg connection, not a
                # shared application-pool lease. Do not close the stats reader.
                close = getattr(application, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        raise OSError("dedicated pooler reader cleanup unavailable") from None


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
