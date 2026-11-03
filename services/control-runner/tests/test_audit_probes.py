import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import botocore.session
import pytest
from botocore.config import Config
from botocore.stub import Stubber

from bravo_controls.audit_probes import (
    AuditBatch,
    AuditEvent,
    AuditSearchProbe,
    CanaryReceipt,
    CloudWatchAuditSearch,
)
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec

START = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
CTX = RunContext("run-1", "demo-chat", Environment("source", "source", "aws:eks/test"))
MARKER = "bravo_audit_" + "a" * 32
ARN = "arn:aws:logs:ap-northeast-2:123456789012:log-group:example-audit"
STREAMS = ("example-stream",)


def receipt(test="AUD-01.1"):
    return CanaryReceipt(
        CTX,
        test,
        MARKER,
        "postgres",
        "chat",
        "source-dev",
        "bravo_test",
        START,
        START + timedelta(seconds=1),
        START + timedelta(minutes=10),
        frozenset({"login"} if test.endswith(".1") else {"create", "delete"}),
    )


def login():
    return {
        "logger": "postgres",
        "record": {
            "user_name": "postgres",
            "database_name": "chat",
            "application_name": MARKER,
            "error_severity": "LOG",
            "message": "connection authorized: user=postgres database=chat application_name="
            + MARKER,
        },
    }


def ddl(action):
    return {
        "logger": "pgaudit",
        "record": {
            "user_name": "postgres",
            "database_name": "chat",
            "application_name": MARKER,
            "audit": {
                "audit_type": "SESSION",
                "class": "DDL",
                "object_type": "TABLE",
                "object_name": "bravo_test." + MARKER,
                "command": "CREATE TABLE" if action == "create" else "DROP TABLE",
                "statement": "sensitive SQL must not enter evidence",
            },
        },
    }


def rbac(action):
    return {
        "kind": "Event",
        "apiVersion": "audit.k8s.io/v1",
        "stage": "ResponseComplete",
        "verb": action,
        "user": {"username": "postgres"},
        "auditID": "request-" + action,
        "stageTimestamp": (START + timedelta(seconds=2)).isoformat(),
        "objectRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "resource": "rolebindings",
            "namespace": "source-dev",
            "name": MARKER,
        },
        "responseStatus": {"code": 201 if action == "create" else 200},
    }


def row(document, id="1", **kw):
    return AuditEvent(
        id,
        STREAMS[0],
        START + timedelta(seconds=2),
        START + timedelta(seconds=3),
        json.dumps(document),
        **kw,
    )


def run(documents, test="AUD-01.1", now=START + timedelta(minutes=10), mutate=None):
    rec = receipt(test)
    batch = AuditBatch(
        CTX,
        MARKER,
        ARN,
        STREAMS,
        START,
        min(now, rec.deadline),
        now,
        tuple(row(d, str(i)) for i, d in enumerate(documents)),
        True,
    )
    if mutate:
        batch = mutate(batch)
    saved = []
    probe = AuditSearchProbe(
        receipt_provider=lambda c, s: rec,
        search=lambda r, t: batch,
        source_binding=ARN,
        streams=STREAMS,
        evidence_sink=saved.append,
        clock=lambda: now,
    )
    result = probe(CTX, Spec(test, "AUD-01", "1", "present", "not_applicable"))
    return result, saved


@pytest.mark.parametrize(
    "test,documents",
    [
        ("AUD-01.1", [login()]),
        ("AUD-01.2", [ddl("create"), ddl("delete")]),
        ("AUD-01.3", [rbac("create"), rbac("delete")]),
    ],
)
def test_semantic_events_present_and_private(test, documents):
    result, saved = run(documents, test)
    assert result.observed == "present"
    assert result.unknown_reason is None
    assert len(saved) == 1
    for private in (MARKER, "postgres", "sensitive SQL", "123456789012", "request-create"):
        assert private.encode() not in saved[0]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(logger="application"),
        lambda d: d["record"].update(user_name="other"),
        lambda d: d["record"].update(database_name="other"),
        lambda d: d["record"].update(application_name=MARKER + "extra"),
        lambda d: d["record"].update(error_severity="ERROR"),
        lambda d: d["record"].update(message="statement: SELECT 'connection authorized: '"),
    ],
)
def test_login_substring_or_wrong_identity_does_not_pass(mutation):
    document = login()
    mutation(document)
    result, _ = run([document])
    assert result.observed == "absent"


@pytest.mark.parametrize("tls", [False, True])
def test_pg18_authorization_precedes_structured_application_name(tls):
    document = login()
    del document["record"]["application_name"]
    if tls:
        document["record"]["message"] += (
            " SSL enabled (protocol=TLSv1.3, cipher=TLS_AES_256_GCM_SHA384, bits=256)"
        )
    assert run([document])[0].observed == "present"


@pytest.mark.parametrize("suffix", ["extra", "\nstatement: secret", " application_name=other"])
def test_authorization_message_exact_marker_boundary(suffix):
    document = login()
    del document["record"]["application_name"]
    document["record"]["message"] += suffix
    assert run([document])[0].observed == "absent"


@pytest.mark.parametrize(
    "field,value",
    [
        ("class", "READ"),
        ("object_type", "INDEX"),
        ("object_name", "other." + MARKER),
        ("command", "SELECT"),
        ("audit_type", "OBJECT"),
    ],
)
def test_ddl_requires_structured_event_not_statement_match(field, value):
    doc = ddl("create")
    doc["record"]["audit"][field] = value
    result, _ = run([doc, ddl("delete")], "AUD-01.2")
    assert result.observed == "absent"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(stage="RequestReceived"),
        lambda d: d.update(verb="patch"),
        lambda d: d.update(impersonatedUser={"username": "postgres"}),
        lambda d: d["responseStatus"].update(code=403),
        lambda d: d["responseStatus"].update(code=True),
        lambda d: d["objectRef"].update(namespace="other"),
        lambda d: d["objectRef"].update(name=MARKER + "extra"),
        lambda d: d["objectRef"].update(resource="secrets"),
        lambda d: d["user"].update(username="other"),
        lambda d: d.update(stageTimestamp=(START - timedelta(days=1)).isoformat()),
    ],
)
def test_rbac_failed_or_wrong_request_not_present(mutation):
    document = rbac("create")
    mutation(document)
    assert run([document, rbac("delete")], "AUD-01.3")[0].observed == "absent"


def test_missing_events_pending_then_absent_after_window():
    early, _ = run([], now=START + timedelta(seconds=5))
    assert early.observed is None and early.unknown_reason == "NOT_RUN"
    final, _ = run([])
    assert final.observed == "absent" and final.unknown_reason is None


def test_create_without_delete_never_completes():
    assert run([ddl("create")], "AUD-01.2")[0].observed == "absent"
    assert run([rbac("create")], "AUD-01.3")[0].observed == "absent"


@pytest.mark.parametrize(
    "changes",
    [
        {"complete": False},
        {"source_binding": "other"},
        {"streams": ("other",)},
        {"marker": MARKER + "other"},
        {"context": replace(CTX, app="other")},
        {"through": START},
        {"queried_at": START - timedelta(days=1)},
    ],
)
def test_search_binding_and_completeness_required(changes):
    with pytest.raises(ValueError):
        run([login()], mutate=lambda b: replace(b, **changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"stream": "other"},
        {"timestamp": START - timedelta(seconds=1)},
        {"ingested_at": START - timedelta(seconds=1)},
        {"ingested_at": START + timedelta(hours=1)},
        {"message": '{"record":{},"record":{}}'},
        {"message": "not-json"},
    ],
)
def test_invalid_event_cannot_pass(changes):
    with pytest.raises(ValueError):
        run([login()], mutate=lambda b: replace(b, events=(replace(b.events[0], **changes),)))


def test_late_ingestion_not_ten_minute_success():
    result, _ = run(
        [login()],
        now=START + timedelta(minutes=11),
        mutate=lambda b: replace(
            b, events=(replace(b.events[0], ingested_at=START + timedelta(minutes=10, seconds=1)),)
        ),
    )
    assert result.observed == "absent"


def test_duplicate_search_events_rejected():
    with pytest.raises(ValueError):
        run([login()], mutate=lambda b: replace(b, events=b.events * 2))


def test_receipt_requires_successful_effects_and_short_window():
    with pytest.raises(ValueError):
        replace(receipt("AUD-01.2"), acknowledged=frozenset({"create"}))
    with pytest.raises(ValueError):
        replace(receipt(), deadline=START + timedelta(hours=1))
    with pytest.raises(ValueError):
        replace(receipt(), marker='" injection')


def client():
    return botocore.session.get_session().create_client(
        "logs",
        region_name="ap-northeast-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=Config(
            connect_timeout=2, read_timeout=3, retries={"mode": "standard", "total_max_attempts": 1}
        ),
    )


def expected_query(**extra):
    return {
        "logGroupIdentifier": ARN,
        "logStreamNames": list(STREAMS),
        "filterPattern": '"' + MARKER + '"',
        "unmask": False,
        "limit": 1000,
        "startTime": int(START.timestamp() * 1000),
        "endTime": int((START + timedelta(minutes=10)).timestamp() * 1000),
        **extra,
    }


def page(events=None, **extra):
    return {
        "events": [] if events is None else events,
        "ResponseMetadata": {"HTTPStatusCode": 200},
        **extra,
    }


def test_cloudwatch_follows_empty_pages():
    c = client()
    probe = CloudWatchAuditSearch(
        c, log_group_arn=ARN, streams=STREAMS, clock=lambda: START + timedelta(minutes=10)
    )
    with Stubber(c) as stub:
        stub.add_response("filter_log_events", page(nextToken="next"), expected_query())
        stub.add_response("filter_log_events", page(), expected_query(nextToken="next"))
        batch = probe(receipt(), receipt().deadline)
        stub.assert_no_pending_responses()
    assert batch.complete and not batch.events


def test_cloudwatch_refuses_repeated_tokens():
    c = client()
    with Stubber(c) as stub:
        stub.add_response("filter_log_events", page(nextToken="same"), expected_query())
        stub.add_response(
            "filter_log_events", page(nextToken="same"), expected_query(nextToken="same")
        )
        with pytest.raises(ValueError, match="pagination"):
            CloudWatchAuditSearch(c, log_group_arn=ARN, streams=STREAMS)(
                receipt(), receipt().deadline
            )


def test_cloudwatch_access_denied_is_unknown_not_negative_pass():
    c = client()
    with Stubber(c) as stub:
        stub.add_client_error(
            "filter_log_events",
            "AccessDeniedException",
            "private",
            400,
            expected_params=expected_query(),
        )
        with pytest.raises(PermissionError, match="search denied"):
            CloudWatchAuditSearch(c, log_group_arn=ARN, streams=STREAMS)(
                receipt(), receipt().deadline
            )


def test_cloudwatch_midstream_error_discards_partial_search():
    c = client()
    with Stubber(c) as stub:
        stub.add_response("filter_log_events", page(nextToken="next"), expected_query())
        stub.add_client_error(
            "filter_log_events",
            "ServiceUnavailableException",
            "private",
            500,
            expected_params=expected_query(nextToken="next"),
        )
        with pytest.raises(ValueError, match="provider search failed"):
            CloudWatchAuditSearch(c, log_group_arn=ARN, streams=STREAMS)(
                receipt(), receipt().deadline
            )


def test_cloudwatch_success_envelope():
    c = client()
    event = {
        "eventId": "e1",
        "logStreamName": STREAMS[0],
        "message": json.dumps(login()),
        "timestamp": int((START + timedelta(seconds=2)).timestamp() * 1000),
        "ingestionTime": int((START + timedelta(seconds=3)).timestamp() * 1000),
    }
    with Stubber(c) as stub:
        stub.add_response("filter_log_events", page([event]), expected_query())
        result = CloudWatchAuditSearch(
            c, log_group_arn=ARN, streams=STREAMS, clock=lambda: START + timedelta(minutes=10)
        )(receipt(), receipt().deadline)
    assert result.events[0].id == "e1"
    assert "connection authorized" not in repr(result)


def test_search_identity_or_bounds_not_mutable():
    c = client()
    with pytest.raises(ValueError):
        CloudWatchAuditSearch(c, log_group_arn=ARN, streams=())
    with pytest.raises(ValueError):
        CloudWatchAuditSearch(
            c, log_group_arn=ARN.replace("ap-northeast-2", "eu-west-1"), streams=STREAMS
        )
    with pytest.raises(ValueError):
        CloudWatchAuditSearch(c, log_group_arn=ARN, streams=STREAMS)(
            receipt(), START - timedelta(seconds=1)
        )


def test_sink_failure_cannot_return_success():
    rec = receipt()
    now = rec.deadline
    batch = AuditBatch(CTX, MARKER, ARN, STREAMS, START, now, now, (row(login()),), True)

    def failed_sink(_):
        raise OSError("evidence storage unavailable")

    probe = AuditSearchProbe(
        receipt_provider=lambda c, s: rec,
        search=lambda r, t: batch,
        source_binding=ARN,
        streams=STREAMS,
        evidence_sink=failed_sink,
        clock=lambda: now,
    )
    with pytest.raises(OSError):
        probe(CTX, Spec("AUD-01.1", "AUD-01", "1", "present", "not_applicable"))
