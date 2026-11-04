import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bravo_controls.audit_probes import AuditBatch, AuditSearchProbe, CanaryReceipt
from bravo_controls.otlp_audit import AuditOrigin, decode_export
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
MARKER = "bravo_audit_" + "a" * 32
ORIGIN = AuditOrigin(
    "source",
    "sha256:" + "a" * 64,
    "22222222-2222-4222-8222-222222222222",
    "11111111-1111-4111-8111-111111111111",
    "audit-test",
    "audit-db-1",
    True,
)


def value(item):
    if isinstance(item, dict):
        return {"kvlistValue": {"values": [{"key": k, "value": value(v)} for k, v in item.items()]}}
    if type(item) is int:
        return {"intValue": str(item)}
    return {"stringValue": item}


def event(kind="cnpg", body=None):
    if body is None:
        body = {
            "logger": "postgres",
            "record": {
                "user_name": "audit_role",
                "database_name": "chat",
                "error_severity": "LOG",
                "message": f"connection authorized: user=audit_role database=chat application_name={MARKER}",
            },
        }
    attrs = [{"key": k, "value": value(v)} for k, v in ORIGIN.attributes(kind).items()]
    row = {
        "timeUnixNano": str(int((NOW - timedelta(seconds=1)).timestamp()) * 10**9),
        "observedTimeUnixNano": "1",
        "body": value(body),
    }
    return {
        "resourceLogs": [
            {"resource": {"attributes": attrs}, "scopeLogs": [{"scope": {}, "logRecords": [row]}]}
        ]
    }


def decode(document, **kwargs):
    return decode_export(
        json.dumps(document).encode(),
        origin=ORIGIN,
        received_at=kwargs.get("received_at", NOW),
        receipt_id="stored-receipt-1",
    )


def row(document):
    return document["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]


def test_ingestion_time_is_from_trusted_receipt_not_sender():
    result = decode(event())[0]
    assert result.ingested_at == NOW
    assert result.timestamp == NOW - timedelta(seconds=1)
    assert result.stream == ORIGIN.streams[0]
    assert json.loads(result.message)["logger"] == "postgres"
    assert decode(event()) == decode(event())


@pytest.mark.parametrize(
    "name",
    [
        "bravo.environment",
        "bravo.audit.registration",
        "k8s.node.uid",
        "k8s.pod.uid",
        "k8s.namespace.name",
        "k8s.pod.name",
        "k8s.container.name",
    ],
)
def test_origin_mismatch_rejected(name):
    document = event()
    attrs = document["resourceLogs"][0]["resource"]["attributes"]
    next(a for a in attrs if a["key"] == name)["value"] = value("wrong")
    with pytest.raises(ValueError):
        decode(document)


def test_duplicate_resource_key_rejected():
    document = event()
    attrs = document["resourceLogs"][0]["resource"]["attributes"]
    attrs.append(copy.deepcopy(attrs[0]))
    with pytest.raises(ValueError):
        decode(document)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        [],
        {"resourceLogs": []},
        {"resourceLogs": None},
        {"resourceLogs": [], "partialSuccess": {}},
    ],
)
def test_bad_envelopes_rejected(bad):
    with pytest.raises(ValueError):
        decode(bad)


@pytest.mark.parametrize(
    "data",
    [
        b'{"resourceLogs":[],"resourceLogs":[]}',
        b"NaN",
        b"\xff",
        b"{" * 2000,
        b" " * (1024 * 1024 + 1),
    ],
)
def test_duplicate_json_invalid_encoding_depth_and_size(data):
    with pytest.raises(ValueError):
        decode_export(data, origin=ORIGIN, received_at=NOW, receipt_id="receipt")


@pytest.mark.parametrize("location", ["resource", "scope", "record"])
def test_dropped_metadata_rejected(location):
    document = event()
    resource = document["resourceLogs"][0]
    target = (
        resource["resource"]
        if location == "resource"
        else (resource["scopeLogs"][0]["scope"] if location == "scope" else row(document))
    )
    target["droppedAttributesCount"] = 1
    with pytest.raises(ValueError):
        decode(document)


@pytest.mark.parametrize("bad", ["0", "-1", "1e9", 100, str(2**64), True, None])
def test_missing_or_noncanonical_event_time(bad):
    document = event()
    row(document)["timeUnixNano"] = bad
    with pytest.raises(ValueError):
        decode(document)


def test_future_event_rejected_and_old_event_preserved_for_search_window():
    document = event()
    with pytest.raises(ValueError):
        decode(document, received_at=NOW - timedelta(seconds=2))
    assert decode(document)[0].timestamp < NOW


@pytest.mark.parametrize(
    "bad",
    [
        {"stringValue": "not an object"},
        {"bytesValue": "YWJj"},
        {"intValue": str(2**63)},
        {"intValue": "1", "stringValue": "1"},
        {"doubleValue": 1.0},
        {"arrayValue": {"values": []}},
    ],
)
def test_unsupported_or_ambiguous_body_rejected(bad):
    document = event()
    row(document)["body"] = bad
    with pytest.raises(ValueError):
        decode(document)


def test_duplicate_nested_body_key_rejected():
    document = event()
    pairs = row(document)["body"]["kvlistValue"]["values"]
    pairs.append(copy.deepcopy(pairs[0]))
    with pytest.raises(ValueError):
        decode(document)


def test_no_partial_results_when_later_record_is_bad():
    document = event()
    records = document["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    records.append({})
    with pytest.raises(ValueError):
        decode(document)


def test_record_limit():
    document = event()
    records = document["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    records[:] = records * 1001
    with pytest.raises(ValueError):
        decode(document)


def kube(verb):
    return {
        "apiVersion": "audit.k8s.io/v1",
        "kind": "Event",
        "stage": "ResponseComplete",
        "auditID": "canary-" + verb,
        "verb": verb,
        "user": {"username": "audit_role"},
        "objectRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "resource": "rolebindings",
            "namespace": "audit-test",
            "name": MARKER,
        },
        "responseStatus": {"code": 201 if verb == "create" else 200},
        "stageTimestamp": (NOW - timedelta(seconds=1)).isoformat(),
    }


def test_kubernetes_time_comes_from_api_server_stage_not_file_read():
    document = event("kubernetes", kube("create"))
    row(document)["timeUnixNano"] = "0"
    result = decode(document)[0]
    assert result.timestamp == NOW - timedelta(seconds=1)
    assert result.stream == ORIGIN.streams[1]
    assert json.loads(result.message)["responseStatus"]["code"] == 201
    with pytest.raises(ValueError):
        decode_export(
            json.dumps(document).encode(),
            origin=replace(ORIGIN, include_node_audit=False),
            received_at=NOW,
            receipt_id="receipt",
        )


@pytest.mark.parametrize("test_id", ["AUD-01.1", "AUD-01.2", "AUD-01.3"])
@pytest.mark.parametrize("late", [False, True])
def test_decoded_records_feed_existing_semantic_audit_probe(test_id, late):
    queried = NOW + timedelta(seconds=60) if late else NOW
    context = RunContext("run-1", "chat", Environment("source", "source", "k8s:registered-source"))
    actions = frozenset({"login"} if test_id == "AUD-01.1" else {"create", "delete"})
    receipt = CanaryReceipt(
        context,
        test_id,
        MARKER,
        "audit_role",
        "chat",
        "audit-test",
        "bravo_test",
        NOW - timedelta(seconds=2),
        NOW - timedelta(microseconds=1),
        NOW + timedelta(seconds=30),
        actions,
    )
    documents = [event()]
    if test_id == "AUD-01.3":
        documents = [event("kubernetes", kube(verb)) for verb in ("create", "delete")]
    elif test_id == "AUD-01.2":
        documents = [
            event(
                body={
                    "logger": "pgaudit",
                    "record": {
                        "user_name": "audit_role",
                        "database_name": "chat",
                        "application_name": MARKER,
                        "audit": {
                            "audit_type": "SESSION",
                            "class": "DDL",
                            "object_type": "TABLE",
                            "object_name": "bravo_test." + MARKER,
                            "command": command,
                        },
                    },
                }
            )
            for command in ("CREATE TABLE", "DROP TABLE")
        ]
    events = tuple(
        e
        for i, document in enumerate(documents)
        for e in decode_export(
            json.dumps(document).encode(),
            origin=ORIGIN,
            received_at=queried,
            receipt_id=f"receipt-{i}",
        )
    )
    evidence = []
    probe = AuditSearchProbe(
        receipt_provider=lambda c, s: receipt,
        search=lambda r, through: AuditBatch(
            context,
            MARKER,
            ORIGIN.registration_digest,
            ORIGIN.streams,
            r.started_at,
            through,
            queried,
            events,
            True,
        ),
        source_binding=ORIGIN.registration_digest,
        streams=ORIGIN.streams,
        evidence_sink=evidence.append,
        clock=lambda: queried,
    )
    result = probe(context, Spec(test_id, "AUD-01", "1", "present", "not_applicable"))
    assert result.observed == ("absent" if late else "present")
    assert MARKER.encode() not in evidence[0]


def test_late_receiver_ack_cannot_be_replaced_by_earlier_sender_time():
    document = event()
    row(document)["observedTimeUnixNano"] = "1"
    received = NOW + timedelta(minutes=5)
    assert decode(document, received_at=received)[0].ingested_at == received


def test_unredacted_attributes_rejected():
    document = event()
    row(document)["attributes"] = [{"key": "secret", "value": value("sensitive")}]
    with pytest.raises(ValueError):
        decode(document)


@pytest.mark.parametrize("number", [200, 201.0])
def test_collector_integral_double_status_preserved(number):
    document = event("kubernetes", kube("create"))
    fields = row(document)["body"]["kvlistValue"]["values"]
    status = next(v for v in fields if v["key"] == "responseStatus")
    status["value"]["kvlistValue"]["values"][0]["value"] = {"doubleValue": number}
    assert json.loads(decode(document)[0].message)["responseStatus"]["code"] == int(number)


@pytest.mark.parametrize("number", [201.5, 2**53, True, "201", float("inf")])
def test_fractional_or_inexact_double_not_coerced(number):
    document = event("kubernetes", kube("create"))
    fields = row(document)["body"]["kvlistValue"]["values"]
    status = next(v for v in fields if v["key"] == "responseStatus")
    status["value"]["kvlistValue"]["values"][0]["value"] = {"doubleValue": number}
    with pytest.raises(ValueError):
        decode(document)
