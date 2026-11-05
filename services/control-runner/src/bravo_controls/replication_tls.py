"""ENC-01.4: bind logical subscription settings to its actual publisher TLS sender.

Only registered PostgreSQL pgoutput subscriptions are supported. No replication
configuration, slot, session or application data is changed by the collector.
"""

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .runner import SHA256, Environment, Observation, RunContext, TestSpec

SPEC = TestSpec("ENC-01.4", "ENC-01", "1", "enabled", "not_applicable")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
SUBSCRIPTION_SQL = """
SELECT s.oid, s.subname, s.subenabled, s.subslotname,
       CASE WHEN octet_length(s.subconninfo) <= 16384 THEN s.subconninfo ELSE NULL END
FROM pg_catalog.pg_subscription s
JOIN pg_catalog.pg_database d ON d.oid = s.subdbid
WHERE s.subname = %s AND d.datname = current_database()
LIMIT 2
"""
SENDER_SQL = """
SELECT s.slot_name, s.slot_type, s.plugin, s.database, s.active, s.active_pid,
       r.pid, r.usename, r.application_name, pg_catalog.host(r.client_addr), r.backend_start,
       r.state, t.ssl, t.version
FROM pg_catalog.pg_replication_slots s
LEFT JOIN pg_catalog.pg_stat_replication r ON r.pid = s.active_pid
LEFT JOIN pg_catalog.pg_stat_ssl t ON t.pid = r.pid
WHERE s.slot_name = %s
LIMIT 2
"""


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware replication timestamp required")
    return value.astimezone(UTC)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, repr=False)
class ReplicationTarget:
    app: str
    environment: Environment
    publisher_uid: str
    subscriber_uid: str
    publisher_database: str
    subscriber_database: str
    subscription: str
    slot: str
    username: str
    application_name: str
    publisher_host: str
    publisher_port: int
    subscriber_address: str
    ca_path: str
    registration_sha256: str
    # Trusted T8 applied-configuration receipt, never the time of this inspection.
    configured_at: datetime

    def __post_init__(self):
        if (not isinstance(self.environment, Environment)
                or any(not isinstance(v, str) or not NAME.fullmatch(v) for v in (
                    self.app, self.publisher_uid, self.subscriber_uid, self.publisher_database,
                    self.subscriber_database, self.subscription, self.slot, self.username,
                    self.application_name, self.publisher_host))
                or self.publisher_uid == self.subscriber_uid
                or type(self.publisher_port) is not int or not 1 <= self.publisher_port <= 65535
                or not isinstance(self.subscriber_address, str)
                or not isinstance(self.ca_path, str) or not self.ca_path.startswith("/")
                or len(self.ca_path) > 512 or any(ord(c) < 32 for c in self.ca_path)
                or not isinstance(self.registration_sha256, str)
                or not SHA256.fullmatch(self.registration_sha256)):
            raise ValueError("registered logical replication path required")
        ipaddress.ip_address(self.subscriber_address)
        utc(self.configured_at)

    def binding(self):
        return {k: (v.isoformat() if isinstance(v, datetime) else v)
                for k, v in vars(self).items() if k not in {"environment", "app"}}


def subscription_facts(rows, target):
    """Parse conninfo in memory, retaining only approved nonsecret attributes."""
    from psycopg.conninfo import conninfo_to_dict

    try:
        if not isinstance(rows, list) or len(rows) != 1 or len(rows[0]) != 5:
            raise ValueError
        oid, name, enabled, slot, conninfo = rows[0]
        if (type(oid) is not int or oid <= 0 or name != target.subscription
                or type(enabled) is not bool or slot != target.slot
                or not isinstance(conninfo, str) or not 1 <= len(conninfo.encode()) <= 16384):
            raise ValueError
        settings = conninfo_to_dict(conninfo)
        if (settings.get("host") != target.publisher_host
                or settings.get("port") != str(target.publisher_port)
                or settings.get("dbname") != target.publisher_database
                or settings.get("user") != target.username
                or settings.get("application_name", target.subscription) != target.application_name
                or settings.get("sslrootcert") != target.ca_path
                or settings.get("sslmode") not in MODES
                or any(k in settings for k in ("service", "hostaddr", "replication"))):
            raise ValueError
        return {"oid": oid, "enabled": enabled, "sslmode": settings["sslmode"]}
    except Exception:
        # libpq diagnostics can contain the original conninfo/password.
        raise ValueError("subscription readback unqualified") from None


def sender_facts(rows, target, observed_at):
    if not isinstance(rows, list) or len(rows) != 1 or len(rows[0]) != 14:
        raise ValueError("one active registered sender required")
    (slot, kind, plugin, database, active, active_pid, pid, username, appname,
     address, started, state, ssl, version) = rows[0]
    if (slot != target.slot or kind != "logical" or plugin != "pgoutput"
            or database != target.publisher_database or active is not True
            or type(active_pid) is not int or type(pid) is not int or pid <= 0 or pid != active_pid
            or username != target.username or appname != target.application_name
            or not isinstance(address, str)
            or ipaddress.ip_address(address) != ipaddress.ip_address(target.subscriber_address)
            or not utc(target.configured_at) <= utc(started) <= utc(observed_at)
            or state != "streaming" or type(ssl) is not bool
            or (ssl and version not in {"TLSv1", "TLSv1.1", "TLSv1.2", "TLSv1.3"})
            or (not ssl and version is not None)):
        raise ValueError("sender identity or connection epoch unconfirmed")
    return {"pid": pid, "backend_started_at": utc(started).isoformat(),
            "ssl": ssl, "protocol": version}


class PostgreSQLReplicationReader:
    """Fixed queries on dedicated, idle, verify-full psycopg connections.

Host owns connection creation/deadlines and authenticates both resource UIDs
before and after the call. It must not pass shared app/control transactions.
"""

    def __init__(self, publisher, subscriber, *, read_registration,
                 clock=lambda: datetime.now(UTC)):
        if not callable(read_registration) or not callable(clock):
            raise ValueError("authenticated registration readback required")
        self.publisher, self.subscriber = publisher, subscriber
        self.read_registration, self.clock = read_registration, clock

    @staticmethod
    def _query(connection, database, sql, params):
        from psycopg import Connection
        from psycopg.errors import InsufficientPrivilege, QueryCanceled
        from psycopg.pq import TransactionStatus

        if (not isinstance(connection, Connection) or connection.closed
                or connection.info.transaction_status != TransactionStatus.IDLE
                or connection.info.get_parameters().get("sslmode") != "verify-full"
                or connection.info.dbname != database or not connection.pgconn.ssl_in_use):
            raise ValueError("dedicated verified database reader required")
        try:
            with connection.transaction():
                with connection.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = '3s'")
                    cur.execute("SET LOCAL lock_timeout = '1s'")
                    cur.execute("SET LOCAL TIME ZONE 'UTC'")
                    cur.execute(sql, params)
                    return cur.fetchmany(2)
        except InsufficientPrivilege:
            raise PermissionError("replication metadata read denied") from None
        except QueryCanceled:
            raise TimeoutError("replication metadata query timed out") from None

    def __call__(self, context, target, deadline):
        if self.publisher is self.subscriber:
            raise ValueError("distinct replication readers required")

        def check():
            if utc(self.clock()) >= deadline:
                raise TimeoutError("replication read budget exceeded")

        def registration():
            check()
            if self.read_registration(context, target) != target.binding():
                raise ValueError("replication registration changed")
            check()

        def subscription():
            check()
            result = subscription_facts(self._query(self.subscriber, target.subscriber_database,
                                                   SUBSCRIPTION_SQL, (target.subscription,)), target)
            check()
            return result

        def sender():
            check()
            rows = self._query(self.publisher, target.publisher_database, SENDER_SQL, (target.slot,))
            check()
            return sender_facts(rows, target, utc(self.clock()))

        registration()
        before = subscription()
        first, last = sender(), sender()
        after = subscription()
        registration()
        if before != after or first != last or before["enabled"] is not True:
            raise ValueError("replication configuration or sender changed")
        return {"run_id": context.run_id, "binding": target.binding(),
                "subscription": before, "sender": first, "collected_at": utc(self.clock())}


class ReplicationTLSProbe:
    def __init__(self, target, *, collector, evidence_sink, clock=lambda: datetime.now(UTC)):
        if not isinstance(target, ReplicationTarget) or not all(callable(f) for f in (collector, evidence_sink, clock)):
            raise ValueError("registered replication collector required")
        self.target, self.collector, self.sink, self.clock = target, collector, evidence_sink, clock

    def __call__(self, context, spec):
        if (not isinstance(context, RunContext) or spec != SPEC or context.app != self.target.app
                or context.environment != self.target.environment):
            raise ValueError("replication method or scope mismatch")
        started = utc(self.clock())
        observed, reason, facts = None, None, {}
        try:
            report = self.collector(context, self.target, started + timedelta(seconds=30))
            finished = utc(self.clock())
            if (not isinstance(report, dict) or set(report) != {"run_id", "binding", "subscription", "sender", "collected_at"}
                    or report["run_id"] != context.run_id or report["binding"] != self.target.binding()
                    or not started <= utc(report["collected_at"]) <= finished <= started + timedelta(seconds=30)):
                raise ValueError("invalid replication readback")
            sub, sender = report["subscription"], report["sender"]
            if (not isinstance(sub, dict) or set(sub) != {"oid", "enabled", "sslmode"}
                    or sub["enabled"] is not True or type(sub["oid"]) is not int or sub["oid"] <= 0
                    or sub["sslmode"] not in MODES
                    or not isinstance(sender, dict) or set(sender) != {"pid", "backend_started_at", "ssl", "protocol"}
                    or type(sender["pid"]) is not int or sender["pid"] <= 0
                    or type(sender["ssl"]) is not bool
                    or (sender["ssl"] and sender["protocol"] not in {"TLSv1", "TLSv1.1", "TLSv1.2", "TLSv1.3"})
                    or (not sender["ssl"] and sender["protocol"] is not None)
                    or not utc(self.target.configured_at) <= utc(datetime.fromisoformat(sender["backend_started_at"])) <= report["collected_at"]):
                raise ValueError("invalid replication TLS facts")
            facts = {"sslmode": sub["sslmode"], "ssl": sender["ssl"], "protocol": sender["protocol"],
                     "sender_bound": True, "configuration_unchanged": True}
            secure = sub["sslmode"] == "verify-full" and sender["ssl"] and sender["protocol"] in {"TLSv1.2", "TLSv1.3"}
            observed = "enabled" if secure else "disabled"
        except Exception as error:
            finished = utc(self.clock())
            reason = ("TIMEOUT" if isinstance(error, TimeoutError) else "OBSERVATION_DENIED"
                      if isinstance(error, PermissionError) else "TOOL_ERROR")
        payload = encoded({"format_version": "1", "test_id": SPEC.test_id,
                           "run_id": context.run_id, "app": context.app,
                           "environment": {"id": context.environment.id, "role": context.environment.role,
                                           "ref": context.environment.ref},
                           "binding_sha256": digest(encoded(self.target.binding())),
                           "started_at": started.isoformat(), "collected_at": finished.isoformat(),
                           "observed": observed, "unknown_reason": reason, "facts": facts})
        self.sink(payload)
        return Observation(context, SPEC.test_id, "1", observed, "not_applicable", finished,
                           "bravo-replication-tls", "1", digest(payload), reason)
