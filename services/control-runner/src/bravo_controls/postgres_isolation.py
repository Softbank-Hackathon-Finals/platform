"""ISO-01.1 PostgreSQL CONNECT test using psycopg and trusted server JSON logs.

Connection exceptions alone are never a permission-denial receipt. No grants,
role changes, application writes or server/session termination are performed.
"""

import ipaddress
import json
import re
import select
import socket
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .app_isolation import AccessLeg
from .aws_probes import _canonical, _digest
from .runner import Environment, _identifier, _utc

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")


@dataclass(frozen=True)
class DatabasePair:
    app: str
    other_app: str
    environment: Environment
    cluster_uid: str
    host: str
    hostaddr: str
    port: int
    ca_file: Path
    ca_sha256: str
    owner_database: str
    other_database: str
    owner_user: str
    other_user: str
    owner_database_oid: int
    other_database_oid: int
    owner_role_oid: int
    other_role_oid: int

    def __post_init__(self):
        for value in (self.app, self.other_app, self.cluster_uid):
            _identifier(value)
        if (
            self.app == self.other_app
            or not isinstance(self.environment, Environment)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", self.host)
            or str(ipaddress.ip_address(self.hostaddr)) != self.hostaddr
            or ipaddress.ip_address(self.hostaddr).is_unspecified
            or ipaddress.ip_address(self.hostaddr).is_multicast
            or type(self.port) is not int
            or not 1 <= self.port <= 65535
            or not isinstance(self.ca_file, Path)
            or not self.ca_file.is_absolute()
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.ca_sha256)
            or any(
                not isinstance(v, str) or not NAME.fullmatch(v)
                for v in (
                    self.owner_database,
                    self.other_database,
                    self.owner_user,
                    self.other_user,
                )
            )
            or self.owner_database == self.other_database
            or self.owner_user == self.other_user
            or any(
                type(v) is not int or not 1 <= v <= 4294967295
                for v in (
                    self.owner_database_oid,
                    self.other_database_oid,
                    self.owner_role_oid,
                    self.other_role_oid,
                )
            )
            or self.owner_database_oid == self.other_database_oid
            or self.owner_role_oid == self.other_role_oid
        ):
            raise ValueError("registered distinct databases and identities required")

    @property
    def binding(self):
        value = asdict(self)
        value["ca_file"] = str(self.ca_file)
        return _digest(_canonical(value))


@dataclass(frozen=True)
class AuditWindow:
    cluster_uid: str
    # These are the requested attempt bounds, not file modification times.
    started_at: datetime
    finished_at: datetime
    complete: bool
    lines: tuple[bytes, ...]
    client_endpoint: tuple[str, int]


def permission_receipt(window, pair, application_name, started, finished, client_endpoint):
    """Only an exact FATAL/42501 from the registered PostgreSQL log is denial.

    read_audit owns provenance, delivery drain, least privilege and bounded I/O.
    A log upload, error string, empty window or unrelated authentication failure
    cannot be passed off as a server permission rejection.
    """
    if (
        not isinstance(window, AuditWindow)
        or not isinstance(client_endpoint, tuple)
        or len(client_endpoint) != 2
        or type(client_endpoint[1]) is not int
        or not 1 <= client_endpoint[1] <= 65535
        or window.client_endpoint != client_endpoint
        or window.cluster_uid != pair.cluster_uid
        or window.complete is not True
        or _utc(window.started_at) != started
        or _utc(window.finished_at) != finished
        or not isinstance(window.lines, tuple)
        or len(window.lines) > 2000
        or any(not isinstance(line, bytes) or len(line) > 65536 for line in window.lines)
        or sum(map(len, window.lines)) > 1024 * 1024
    ):
        raise ValueError("unqualified PostgreSQL audit window")
    matches = []
    for line in window.lines:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            raise ValueError("invalid PostgreSQL audit record") from None
        if not isinstance(row, dict):
            raise ValueError("invalid PostgreSQL audit record")
        if row.get("remote_host") != client_endpoint[0] or str(row.get("remote_port")) != str(
            client_endpoint[1]
        ):
            continue
        if row.get("application_name") not in {None, "", application_name}:
            continue
        stamp = row.get("timestamp")
        try:
            observed = _utc(datetime.fromisoformat(stamp.replace(" UTC", "+00:00")))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid PostgreSQL audit time") from None
        if (
            row.get("error_severity") == "FATAL"
            and row.get("state_code") == "42501"
            and row.get("user") == pair.other_user
            and row.get("dbname") == pair.owner_database
            and started.replace(microsecond=(started.microsecond // 1000) * 1000)
            <= observed
            <= finished
        ):
            matches.append(row)
        elif row.get("error_severity") in {"FATAL", "ERROR", "PANIC"}:
            # Conflicting errors for the same connection invalidate a denial claim.
            return None
    if len(matches) != 1:
        return None
    # No message/detail/hint/password, host, SQL or raw line in retained evidence.
    return _digest(
        _canonical(
            {
                "nonce": application_name,
                "state": "42501",
                "client_endpoint_sha256": _digest(_canonical(client_endpoint)),
                "cluster": pair.cluster_uid,
                "time": matches[0]["timestamp"],
            }
        )
    )


class PostgreSQLIsolationReader:
    def __init__(
        self, pair, *, passwords, read_registration, read_audit, clock=lambda: datetime.now(UTC)
    ):
        if not isinstance(pair, DatabasePair) or not all(
            callable(x) for x in (passwords, read_registration, read_audit, clock)
        ):
            raise ValueError("registered PostgreSQL reader required")
        self.pair, self.passwords, self.registration, self.audit, self.clock = (
            pair,
            passwords,
            read_registration,
            read_audit,
            clock,
        )
        self.app, self.other_app, self.environment = pair.app, pair.other_app, pair.environment

    @property
    def binding(self):
        return self.pair.binding

    def _parameters(self, user, database, password, application):

        p = self.pair
        if (
            p.ca_file.is_symlink()
            or _digest(p.ca_file.read_bytes()) != p.ca_sha256
            or not isinstance(password, str)
            or not password
            or len(password) > 4096
        ):
            raise ValueError("PostgreSQL credential or CA unavailable")
        return dict(
            host=p.host,
            hostaddr=p.hostaddr,
            port=p.port,
            dbname=database,
            user=user,
            password=password,
            passfile="/dev/null",
            sslmode="verify-full",
            sslrootcert=str(p.ca_file),
            ssl_min_protocol_version="TLSv1.2",
            sslcertmode="disable",
            require_auth="scram-sha-256",
            channel_binding="require",
            gssencmode="disable",
            application_name=application,
            connect_timeout=3,
            autocommit=True,
            options="-c default_transaction_read_only=on -c statement_timeout=3000 -c lock_timeout=1000",
        )

    def _connect(self, user, database, password, application):
        import psycopg

        return psycopg.connect(**self._parameters(user, database, password, application))

    def _connect_cross(self, user, database, password, application):
        """Use libpq's public async API to retain the actual client endpoint.

        Authentication/TLS/protocol stay in libpq. No error text is parsed.
        Direct server paths only: NAT/pooler translation cannot be guessed.
        """
        import psycopg
        from psycopg import pq
        from psycopg.conninfo import make_conninfo

        parameters = self._parameters(user, database, password, application)
        parameters.pop("autocommit")  # psycopg option, not a libpq connection keyword
        raw = pq.PGconn.connect_start(make_conninfo(**parameters).encode())
        deadline = time.monotonic() + 3
        endpoint = None
        transferred = False
        try:
            while True:
                fd = raw.socket
                if fd >= 0:
                    family = (
                        socket.AF_INET6
                        if ipaddress.ip_address(self.pair.hostaddr).version == 6
                        else socket.AF_INET
                    )
                    with socket.fromfd(fd, family, socket.SOCK_STREAM) as duplicate:
                        address = duplicate.getsockname()
                    if address[1]:
                        candidate = (address[0], address[1])
                        if endpoint is not None and endpoint != candidate:
                            raise ValueError("connection endpoint changed")
                        endpoint = candidate
                status = raw.connect_poll()
                if status == pq.PollingStatus.OK:
                    conn = psycopg.Connection(raw)
                    conn.autocommit = True
                    transferred = True
                    return conn, endpoint
                if status == pq.PollingStatus.FAILED:
                    return None, endpoint
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("database connection deadline exceeded")
                fd = raw.socket
                if fd < 0 or status not in {pq.PollingStatus.READING, pq.PollingStatus.WRITING}:
                    raise ValueError("database connection state unqualified")
                readable, writable, _ = select.select(
                    [fd] if status == pq.PollingStatus.READING else [],
                    [fd] if status == pq.PollingStatus.WRITING else [],
                    [],
                    remaining,
                )
                if not readable and not writable:
                    raise TimeoutError("database connection deadline exceeded")
        finally:
            if not transferred:
                raw.finish()

    @staticmethod
    def _identity(connection, database, user, database_oid, role_oid):
        row = connection.execute("""SELECT current_database(), current_user, session_user,
            (SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database()),
            (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user),
            (SELECT ssl FROM pg_catalog.pg_stat_ssl WHERE pid=pg_backend_pid())""").fetchone()
        return row == (database, user, user, database_oid, role_oid, True)

    def __call__(self, context, nonce):
        import psycopg

        p = self.pair
        if (
            context.app != p.app
            or context.environment != p.environment
            or not re.fullmatch(r"[a-f0-9-]{36}", nonce)
        ):
            raise ValueError("database scope mismatch")
        binding = self.binding
        if self.registration(context) != binding:
            raise ValueError("database registration unavailable")
        # One credential snapshot each: retries/refresh cannot silently change identity.
        owner_password, other_password = self.passwords(p.owner_user), self.passwords(p.other_user)
        appname = "t14-iso-" + nonce

        def positive():
            try:
                for db, user, password, db_oid, role_oid in (
                    (
                        p.owner_database,
                        p.owner_user,
                        owner_password,
                        p.owner_database_oid,
                        p.owner_role_oid,
                    ),
                    (
                        p.other_database,
                        p.other_user,
                        other_password,
                        p.other_database_oid,
                        p.other_role_oid,
                    ),
                ):
                    with self._connect(user, db, password, appname + "-ctl") as conn:
                        if not self._identity(conn, db, user, db_oid, role_oid):
                            return False
                return True
            except psycopg.Error:
                return False

        before = positive()
        outcome, reason, receipt = None, "POSITIVE_CONTROL_FAILED", None
        if before:
            started = _utc(self.clock())
            connection, endpoint = self._connect_cross(
                p.other_user, p.owner_database, other_password, appname
            )
            if connection is not None:
                with connection as conn:
                    if self._identity(
                        conn, p.owner_database, p.other_user, p.owner_database_oid, p.other_role_oid
                    ):
                        outcome, reason = "allow", None
                    else:
                        reason = "UNEXPECTED_RESPONSE"
            elif endpoint is not None:
                finished = _utc(self.clock())
                window = self.audit(context, p, appname, started, finished, endpoint)
                receipt = permission_receipt(window, p, appname, started, finished, endpoint)
                outcome, reason = ("deny", None) if receipt else (None, "UNEXPECTED_RESPONSE")
            else:
                reason = "UNEXPECTED_RESPONSE"
        after = positive() if before else False
        if self.registration(context) != binding:
            raise ValueError("database registration changed")
        if not (before and after):
            outcome, reason = None, "POSITIVE_CONTROL_FAILED"
        return AccessLeg(
            context,
            nonce,
            binding,
            before and after,
            outcome,
            reason,
            _utc(self.clock()),
            {
                "positive_before": before,
                "positive_after": after,
                "permission_receipt_sha256": receipt,
                "cross": outcome,
            },
        )
