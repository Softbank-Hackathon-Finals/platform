import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from psycopg import Connection
from psycopg.errors import InsufficientPrivilege, QueryCanceled
from psycopg.pq import TransactionStatus

from bravo_controls.replication_tls import (
    SENDER_SQL,
    SPEC,
    SUBSCRIPTION_SQL,
    PostgreSQLReplicationReader,
    ReplicationTarget,
    ReplicationTLSProbe,
    sender_facts,
    subscription_facts,
)
from bravo_controls.runner import Environment, RunContext

NOW = datetime(2026, 11, 6, tzinfo=UTC)
ENV = Environment("source", "source", "aws:eks/example")
CTX = RunContext("run-1", "workload-1", ENV)
TARGET = ReplicationTarget("workload-1", ENV, "publisher-uid", "subscriber-uid", "chat", "chat",
                           "chat_sub", "chat_slot", "replicator", "chat_sub", "publisher", 5432,
                           "192.0.2.1", "/tls/ca.crt", "sha256:" + "a" * 64,
                           NOW - timedelta(seconds=2))
CONNINFO = "host=publisher port=5432 dbname=chat user=replicator sslmode=verify-full sslrootcert=/tls/ca.crt password='private fixture'"


def sub(**kw):
    row = dict(oid=11, name="chat_sub", enabled=True, slot="chat_slot", conninfo=CONNINFO)
    row.update(kw)
    return [tuple(row.values())]


def sender(**kw):
    row = dict(slot="chat_slot", kind="logical", plugin="pgoutput", database="chat", active=True,
               active_pid=123, pid=123, username="replicator", appname="chat_sub", address="192.0.2.1",
               started=NOW - timedelta(seconds=1), state="streaming", ssl=True, version="TLSv1.3")
    row.update(kw)
    return [tuple(row.values())]


def connection(rows):
    c = MagicMock(spec=Connection)
    c.closed = False
    c.info.transaction_status = TransactionStatus.IDLE
    c.info.get_parameters.return_value = {"sslmode": "verify-full"}
    c.info.dbname = "chat"
    c.pgconn = MagicMock(ssl_in_use=True)
    cursor = c.cursor.return_value.__enter__.return_value
    cursor.fetchmany.return_value = rows
    return c, cursor


def setup(subrows=None, senderrows=None, registration=None):
    pub, pc = connection(senderrows if senderrows is not None else sender())
    dest, dc = connection(subrows if subrows is not None else sub())
    read = PostgreSQLReplicationReader(pub, dest, read_registration=registration or (lambda *_: TARGET.binding()),
                                       clock=lambda: NOW)
    evidence = []
    p = ReplicationTLSProbe(TARGET, collector=read, evidence_sink=evidence.append, clock=lambda: NOW)
    return p, pc, dc, evidence


def test_actual_sender_join_and_settings_both_required_and_no_conninfo_leak():
    p, pc, dc, evidence = setup()
    r = p(CTX, SPEC)
    assert r.observed == "enabled" and r.unknown_reason is None
    assert "private fixture" not in evidence[0].decode()
    assert "publisher" not in evidence[0].decode()
    for cur, sql, params in ((pc, SENDER_SQL, ("chat_slot",)), (dc, SUBSCRIPTION_SQL, ("chat_sub",))):
        assert cur.fetchmany.call_args.args == (2,)
        assert cur.execute.call_args.args == (sql, params)
        assert cur.execute.call_args_list[0].args == ("SET TRANSACTION READ ONLY",)
    assert json.loads(evidence[0])["facts"]["sender_bound"] is True


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"slot": "other"}, {"oid": True}, {"name": "other"},
    {"conninfo": CONNINFO.replace("host=publisher", "host=foreign")},
    {"conninfo": CONNINFO.replace("port=5432", "port=5433")},
    {"conninfo": CONNINFO.replace("dbname=chat", "dbname=other")},
    {"conninfo": CONNINFO.replace("user=replicator", "user=other")},
    {"conninfo": CONNINFO.replace("/tls/ca.crt", "/tls/other.crt")},
    {"conninfo": CONNINFO + " application_name=other"},
    {"conninfo": CONNINFO + " service=external"},
    {"conninfo": CONNINFO + " hostaddr=192.0.2.2"},
    {"conninfo": CONNINFO.replace("sslmode=verify-full", "")},
    {"conninfo": "password='private fixture"},
])
def test_bad_or_ambiguous_subscription_cannot_pass(changes):
    result = setup(subrows=sub(**changes))[0](CTX, SPEC)
    assert result.observed is None and result.unknown_reason == "TOOL_ERROR"


@pytest.mark.parametrize("changes", [
    {"active": False}, {"active_pid": None}, {"pid": 99}, {"pid": True}, {"kind": "physical"},
    {"plugin": "other"}, {"database": "other"}, {"username": "other"},
    {"appname": "other"}, {"address": "192.0.2.2"}, {"state": "startup"},
    {"started": NOW - timedelta(seconds=3)}, {"started": NOW + timedelta(seconds=1)},
    {"ssl": None}, {"version": "unknown"}, {"ssl": False},
])
def test_foreign_stale_inactive_or_unknown_sender_cannot_pass(changes):
    result = setup(senderrows=sender(**changes))[0](CTX, SPEC)
    assert result.observed is None


@pytest.mark.parametrize("mode,ssl,version", [("require", True, "TLSv1.3"),
    ("verify-full", True, "TLSv1.1"), ("disable", False, None)])
def test_observed_weak_tls_is_fail_not_unknown(mode, ssl, version):
    p, *_ = setup(subrows=sub(conninfo=CONNINFO.replace("verify-full", mode)),
                  senderrows=sender(ssl=ssl, version=version))
    r = p(CTX, SPEC)
    assert r.observed == "disabled" and r.unknown_reason is None


def test_sender_restart_or_catalog_change_during_inspection_is_unknown():
    p, pc, dc, _ = setup()
    pc.fetchmany.side_effect = [sender(), sender(active_pid=124, pid=124)]
    assert p(CTX, SPEC).observed is None
    p, pc, dc, _ = setup()
    dc.fetchmany.side_effect = [sub(), sub(oid=12)]
    assert p(CTX, SPEC).observed is None


def test_resource_registration_change_is_unknown():
    registration = MagicMock(side_effect=[TARGET.binding(), {**TARGET.binding(), "publisher_uid": "other"}])
    assert setup(registration=registration)[0](CTX, SPEC).observed is None


@pytest.mark.parametrize("rows", [[], sub() * 2])
def test_missing_or_duplicate_subscriptions_are_unknown(rows):
    assert setup(subrows=rows)[0](CTX, SPEC).observed is None


@pytest.mark.parametrize("field,value", [("closed", True), ("transaction", TransactionStatus.INTRANS),
    ("mode", "require"), ("tls", False), ("database", "other")])
def test_reader_connection_is_not_an_app_or_unverified_connection(field, value):
    c, cur = connection(sub())
    if field == "closed":
        c.closed = value
    elif field == "transaction":
        c.info.transaction_status = value
    elif field == "mode":
        c.info.get_parameters.return_value = {"sslmode": value}
    elif field == "tls":
        c.pgconn.ssl_in_use = value
    else:
        c.info.dbname = value
    with pytest.raises(ValueError):
        PostgreSQLReplicationReader._query(c, "chat", SUBSCRIPTION_SQL, ("chat_sub",))
    cur.execute.assert_not_called()


def test_error_text_never_published_and_storage_failure_is_not_success():
    def fail(*_):
        raise PermissionError("private fixture")

    evidence = []
    p = ReplicationTLSProbe(TARGET, collector=fail, evidence_sink=evidence.append, clock=lambda: NOW)
    assert p(CTX, SPEC).unknown_reason == "OBSERVATION_DENIED"
    assert "private fixture" not in evidence[0].decode()
    p, *_ = setup()
    p.sink = fail
    with pytest.raises(PermissionError):
        p(CTX, SPEC)


def test_conninfo_parser_exception_is_sanitized():
    with pytest.raises(ValueError, match="^subscription readback unqualified$"):
        subscription_facts(sub(conninfo="password='private fixture"), TARGET)


@pytest.mark.parametrize("error,expected", [(InsufficientPrivilege("private fixture"), PermissionError),
                                          (QueryCanceled("private fixture"), TimeoutError)])
def test_database_errors_keep_fixed_nonsecret_classification(error, expected):
    c, cur = connection(sub())
    cur.execute.side_effect = error
    with pytest.raises(expected) as result:
        PostgreSQLReplicationReader._query(c, "chat", SUBSCRIPTION_SQL, ("chat_sub",))
    assert "private fixture" not in str(result.value)


def test_sender_facts_do_not_include_raw_connection_identifiers():
    result = sender_facts(sender(), TARGET, NOW)
    assert set(result) == {"pid", "backend_started_at", "ssl", "protocol"}
