"""Bounded AWS SDK responses → real normalization/store/view; no cloud mutation."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from bravo_findings.aws_pull import collect_guardduty
from bravo_findings.collectors import run_once
from bravo_findings.guardduty import GuardDutySource
from bravo_findings.ingest import Ingestor
from bravo_findings.normalize import Normalizer
from bravo_findings.rules import Rules, rules_dir
from bravo_findings.store import Store
from bravo_findings.view import View

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
DETECTOR = "a" * 32
T0 = 1_793_786_400_000


def finding(number=1):
    raw = json.loads((rules_dir() / "examples/aws-guardduty-direct.raw.json").read_text())
    raw.pop("_note", None)
    raw.update(
        Id=f"{number:032x}",
        AccountId=ACCOUNT,
        Region=REGION,
        Arn=":".join(
            (
                "arn",
                "aws",
                "guardduty",
                REGION,
                ACCOUNT,
                f"detector/{DETECTOR}/finding/{number:032x}",
            )
        ),
        SchemaVersion="2.0",
    )
    raw["Service"]["DetectorId"] = DETECTOR
    return raw


def fake():
    client = Mock(meta=SimpleNamespace(region_name=REGION))
    client.list_detectors.return_value = {"DetectorIds": [DETECTOR]}
    client.get_detector.return_value = {"Status": "ENABLED"}
    client.list_findings.return_value = {"FindingIds": [finding()["Id"]]}
    client.get_findings.return_value = {"Findings": [finding()]}
    return client


def run(client, store=None):
    store = store or Store()
    result = run_once(
        Ingestor(store, Normalizer()),
        GuardDutySource(client, account_id=ACCOUNT, region=REGION),
        "env-aws",
        lambda: T0,
    )
    return result, store


def test_sdk_pages_archived_sample_and_summary():
    client = boto3.client(
        "guardduty",
        region_name=REGION,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    first, second = finding(1), finding(2)
    second["Service"]["Archived"] = True
    with Stubber(client) as stub:
        stub.add_response("list_detectors", {"DetectorIds": [DETECTOR]}, {"MaxResults": 50})
        stub.add_response(
            "get_detector",
            {
                "Status": "ENABLED",
                "ServiceRole": "example",
                "FindingPublishingFrequency": "SIX_HOURS",
            },
            {"DetectorId": DETECTOR},
        )
        request = dict(
            DetectorId=DETECTOR,
            MaxResults=50,
            FindingCriteria={"Criterion": {"updatedAt": {"Gte": 0, "Lte": T0}}},
        )
        stub.add_response(
            "list_findings", {"FindingIds": [first["Id"]], "NextToken": "page2"}, request
        )
        stub.add_response(
            "get_findings",
            {"Findings": [first]},
            {"DetectorId": DETECTOR, "FindingIds": [first["Id"]]},
        )
        stub.add_response(
            "list_findings", {"FindingIds": [second["Id"]]}, request | {"NextToken": "page2"}
        )
        stub.add_response(
            "get_findings",
            {"Findings": [second]},
            {"DetectorId": DETECTOR, "FindingIds": [second["Id"]]},
        )
        counts, store = run(client)
        stub.assert_no_pending_responses()
    assert counts["created"] == 2
    docs = store.findings(environment_id="env-aws", open_only=False)
    assert {d["status_id"] for d in docs} == {1, 3}  # archived is suppressed, never resolved
    assert all(d["bravo"]["sample"] is True and d["severity_id"] == 3 for d in docs)
    assert store.source("aws_guardduty", "env-aws").cursor == str(T0)
    cells = View(store, Rules()).summary(T0)["environments"][0]["cells"]
    assert cells["THREAT"]["state"] == "OK" and cells["THREAT"]["counts"] == {"3": 1}
    assert cells["ENCRYPTION"]["state"] == "NONE"


@pytest.mark.parametrize(
    "fault",
    [
        "absent",
        "disabled",
        "denied",
        "unknown",
        "duplicate_detector",
        "cycle",
        "missing_page",
        "partial",
        "duplicate_ids",
        "foreign_id",
        "foreign_account",
        "foreign_region",
        "foreign_detector",
        "future_window",
        "late_page_error",
    ],
)
def test_incomplete_source_does_not_replace_records_or_advance_cursor(fault):
    client = fake()
    if fault == "absent":
        client.list_detectors.return_value = {"DetectorIds": []}
    elif fault == "disabled":
        client.get_detector.return_value = {"Status": "DISABLED"}
    elif fault == "denied":
        client.get_detector.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "secret-payload"}}, "GetDetector"
        )
    elif fault == "unknown":
        client.get_detector.return_value = {}
    elif fault == "duplicate_detector":
        client.list_detectors.return_value = {"DetectorIds": [DETECTOR, DETECTOR]}
    elif fault == "cycle":
        client.list_findings.return_value = {"FindingIds": [], "NextToken": "same"}
    elif fault == "missing_page":
        client.list_findings.return_value = {}
    elif fault == "partial":
        client.get_findings.return_value = {"Findings": []}
    elif fault == "duplicate_ids":
        client.list_findings.return_value = {"FindingIds": [finding()["Id"]] * 2}
    elif fault == "foreign_id":
        client.get_findings.return_value = {"Findings": [finding(2)]}
    elif fault.startswith("foreign_"):
        row = finding()
        if fault == "foreign_account":
            row["AccountId"] = "000000000000"
        elif fault == "foreign_region":
            row["Region"] = "us-east-1"
        else:
            row["Service"]["DetectorId"] = "b" * 32
        client.get_findings.return_value = {"Findings": [row]}
    elif fault == "future_window":
        row = finding()
        row["UpdatedAt"] = "2027-01-01T00:00:00Z"
        client.get_findings.return_value = {"Findings": [row]}
    elif fault == "late_page_error":
        client.list_findings.side_effect = [
            {"FindingIds": [finding()["Id"]], "NextToken": "next"},
            TimeoutError(),
        ]
    store = Store()
    store.register_source("aws_guardduty", "env-aws")
    store.record_success("aws_guardduty", "env-aws", T0 - 100, cursor="100")
    counts, store = run(client, store)
    assert counts["failed"] == 1
    row = store.source("aws_guardduty", "env-aws")
    assert row.cursor == "100" and row.last_success_time == T0 - 100
    assert store.findings(open_only=False) == []
    assert (
        View(store, Rules()).summary(T0)["environments"][0]["cells"]["THREAT"]["state"] == "UNKNOWN"
    )
    if fault in {"absent", "disabled"}:
        assert row.error_code == "NOT_ENABLED"
    if fault == "denied":
        assert row.error_code == "ACCESS_DENIED"


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, 2),
        (3.9, 2),
        (4, 3),
        (6.9, 3),
        (7, 4),
        (8.9, 4),
        (9, 5),
        (10, 5),
        (0, 0),
        (10.1, 0),
        (True, 0),
        ("9", 0),
        (None, 0),
        (float("nan"), 0),
        (float("inf"), 0),
    ],
)
def test_numeric_severity_boundaries(value, expected):
    assert Rules().severity("aws_guardduty", {"Severity": value}) == expected


def test_explicit_entrypoint_checks_account_before_detection_and_preserves_hub():
    store = Store()
    session = Mock()
    sts, client = Mock(), fake()
    sts.get_caller_identity.return_value = {"Account": "000000000000"}
    session.client.side_effect = lambda name, **_: sts if name == "sts" else client
    result = collect_guardduty(
        store,
        session,
        account_id=ACCOUNT,
        region=REGION,
        environment_id="env-aws",
        clock=lambda: T0,
    )
    assert result["failed"] == 1
    client.list_detectors.assert_not_called()
    sts.get_caller_identity.return_value = {"Account": ACCOUNT}
    store.register_source("aws_security_hub", "env-aws")
    before = copy.copy(store.source("aws_security_hub", "env-aws"))
    assert (
        collect_guardduty(
            store,
            session,
            account_id=ACCOUNT,
            region=REGION,
            environment_id="env-aws",
            clock=lambda: T0,
        )["failed"]
        == 1
    )
    assert store.source("aws_security_hub", "env-aws") == before
    client.list_detectors.assert_not_called()


def test_source_recovers_after_enable_with_same_cursor_and_bounded_sdk_configuration():
    store = Store()
    client = fake()
    session = Mock()
    sts = Mock()
    sts.get_caller_identity.return_value = {"Account": ACCOUNT}
    session.client.side_effect = lambda name, **_: sts if name == "sts" else client
    client.list_detectors.return_value = {"DetectorIds": []}
    args = dict(account_id=ACCOUNT, region=REGION, environment_id="env-aws", clock=lambda: T0)
    assert collect_guardduty(store, session, **args)["failed"] == 1
    client.list_detectors.return_value = {"DetectorIds": [DETECTOR]}
    assert collect_guardduty(store, session, **args)["created"] == 1
    for call in session.client.call_args_list:
        config = call.kwargs["config"]
        assert config.ignore_configured_endpoint_urls is True and config.proxies == {}
        assert config.retries["total_max_attempts"] == 3
    assert store.source("aws_guardduty", "env-aws").error_code is None
