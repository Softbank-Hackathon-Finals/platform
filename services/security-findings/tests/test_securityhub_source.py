import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from bravo_findings.collectors import run_once
from bravo_findings.ingest import Ingestor
from bravo_findings.normalize import Normalizer
from bravo_findings.rules import Rules, rules_dir
from bravo_findings.securityhub import SecurityHubSource
from bravo_findings.securityhub_pull import collect_securityhub
from bravo_findings.store import Store
from bravo_findings.view import View

ACCOUNT = "123456789012"
REGION = "ap-northeast-1"
T0 = 1_793_786_400_000
HUB = {
    "HubArn": ":".join(("arn", "aws", "securityhub", REGION, ACCOUNT, "hub/default")),
    "SubscribedAt": "2026-11-01T00:00:00Z",
}


def finding():
    raw = json.loads((rules_dir() / "examples/aws-security-hub-control.raw.json").read_text())
    raw.pop("_note", None)
    raw["AwsAccountId"] = ACCOUNT
    return raw


def fake():
    client = Mock(meta=SimpleNamespace(region_name=REGION))
    client.describe_hub.return_value = HUB.copy()
    client.get_findings.return_value = {"Findings": [finding()]}
    return client


def run(client, store=None):
    store = store or Store()
    counts = run_once(
        Ingestor(store, Normalizer()),
        SecurityHubSource(client, account_id=ACCOUNT, region=REGION),
        "env-aws",
        lambda: T0,
    )
    return counts, store


def test_real_sdk_contract_account_region_window_and_empty_page():
    client = boto3.client(
        "securityhub", region_name=REGION, aws_access_key_id="test", aws_secret_access_key="test"
    )
    request = {
        "Filters": {
            "AwsAccountId": [{"Comparison": "EQUALS", "Value": ACCOUNT}],
            "Region": [{"Comparison": "EQUALS", "Value": REGION}],
            "UpdatedAt": [{"Start": "1970-01-01T00:00:00.000Z", "End": "2026-11-04T10:00:00.000Z"}],
        },
        "MaxResults": 100,
    }
    with Stubber(client) as stub:
        stub.add_response("describe_hub", HUB, {})
        stub.add_response("get_findings", {"Findings": [finding()], "NextToken": "p2"}, request)
        stub.add_response("get_findings", {"Findings": []}, request | {"NextToken": "p2"})
        stub.add_response("describe_hub", HUB, {})
        counts, store = run(client)
        stub.assert_no_pending_responses()
    assert counts["created"] == 1
    assert store.source("aws_security_hub", "env-aws").cursor == str(T0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("AwsAccountId", "111122223333"),
        ("Region", "eu-west-1"),
        ("SchemaVersion", "wrong"),
        ("Id", ""),
        ("Id", "\nsecret"),
        ("ProductArn", "https://evil.invalid"),
        ("ProductArn", "arn:aws:securityhub:eu-west-1::product/aws/securityhub"),
        ("UpdatedAt", "2027-01-01T00:00:00Z"),
        ("UpdatedAt", None),
    ],
)
def test_bad_findings_preserve_existing_cursor_and_never_save(field, value):
    client = fake()
    client.get_findings.return_value["Findings"][0][field] = value
    store = Store()
    store.register_source("aws_security_hub", "env-aws")
    store.record_success("aws_security_hub", "env-aws", T0 - 1000, cursor=str(T0 - 1000))
    counts, store = run(client, store)
    assert counts == {"failed": 1}
    assert store.source("aws_security_hub", "env-aws").cursor == str(T0 - 1000)
    assert store.findings(environment_id="env-aws") == []


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"Findings": None},
        {"Findings": {}},
        {"Findings": [None]},
        {"Findings": [], "NextToken": ""},
        {"Findings": [], "NextToken": 1},
        {"Findings": [], "NextToken": "x" * 8193},
    ],
)
def test_malformed_provider_response_is_not_empty_success(page):
    client = fake()
    client.get_findings.return_value = page
    counts, store = run(client)
    assert counts == {"failed": 1}
    assert store.source("aws_security_hub", "env-aws").last_success_time is None


def test_repeated_page_token_and_duplicate_identity_are_atomic():
    for pages in (
        [{"Findings": [], "NextToken": "a"}] * 2,
        [{"Findings": [finding()], "NextToken": "a"}, {"Findings": [finding()]}],
    ):
        client = fake()
        client.get_findings.side_effect = pages
        counts, store = run(client)
        assert counts == {"failed": 1}
        assert store.findings(environment_id="env-aws") == []


def test_page_bound_terminates_even_when_tokens_keep_changing():
    client = fake()
    client.get_findings.side_effect = [{"Findings": [], "NextToken": str(i)} for i in range(201)]
    counts, _ = run(client)
    assert counts == {"failed": 1}
    assert client.get_findings.call_count == 200


@pytest.mark.parametrize(
    "hub",
    [
        {},
        {"HubArn": HUB["HubArn"], "SubscribedAt": "bad"},
        HUB | {"HubArn": HUB["HubArn"].replace(ACCOUNT, "111122223333")},
    ],
)
def test_invalid_hub_prevents_findings_query(hub):
    client = fake()
    client.describe_hub.return_value = hub
    assert run(client)[0] == {"failed": 1}
    client.get_findings.assert_not_called()


def test_subscription_change_during_read_discards_all_rows():
    client = fake()
    client.describe_hub.side_effect = [HUB, HUB | {"SubscribedAt": "2026-11-02T00:00:00Z"}]
    counts, store = run(client)
    assert counts == {"failed": 1}
    assert store.findings(environment_id="env-aws") == []


def test_401_is_not_proof_service_is_disabled_and_error_text_is_not_saved(caplog):
    client = fake()
    client.describe_hub.side_effect = ClientError(
        {
            "Error": {
                "Code": "InvalidAccessException",
                "Message": "not subscribed secret-material",
            },
            "ResponseMetadata": {"HTTPStatusCode": 401},
        },
        "DescribeHub",
    )
    counts, store = run(client)
    assert counts == {"failed": 1}
    assert store.source("aws_security_hub", "env-aws").error_code == "ACCESS_DENIED"
    assert "secret-material" not in caplog.text
    assert View(store, Rules()).statuses(T0)[0]["state"] == "FAILED"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page_size": 0},
        {"page_size": 101},
        {"page_size": True},
        {"account_id": "wrong"},
        {"region": "eu-west-1"},
    ],
)
def test_invalid_registered_scope(kwargs):
    with pytest.raises(ValueError):
        SecurityHubSource(fake(), **({"account_id": ACCOUNT, "region": REGION} | kwargs))


@pytest.mark.parametrize("window", [(True, T0), (-1, T0), (T0, 1)])
def test_invalid_window_performs_no_api_calls(window):
    client = fake()
    with pytest.raises(ValueError):
        list(SecurityHubSource(client, account_id=ACCOUNT, region=REGION).fetch(*window))
    assert not client.method_calls


def test_custom_product_publisher_can_differ_from_affected_account():
    client = fake()
    client.get_findings.return_value["Findings"][0]["ProductArn"] = (
        f"arn:aws:securityhub:{REGION}:111122223333:product/vendor/product"
    )
    assert run(client)[0]["created"] == 1


def test_second_page_failure_preserves_prior_finding_and_cursor():
    client = fake()
    _, store = run(client)
    changed = copy.deepcopy(finding())
    changed["Id"] += "new"
    client.get_findings.side_effect = [
        {"Findings": [changed], "NextToken": "p2"},
        PermissionError("private"),
    ]
    assert run(client, store)[0] == {"failed": 1}
    assert len(store.findings(environment_id="env-aws")) == 1
    assert store.source("aws_security_hub", "env-aws").cursor == str(T0)


def test_explicit_entrypoint_validates_sts_and_ignores_environment_endpoint():
    session = Mock()
    sts, hub = Mock(), fake()
    sts.get_caller_identity.return_value = {"Account": ACCOUNT}
    session.client.side_effect = [sts, hub]
    counts = collect_securityhub(
        Store(),
        session,
        account_id=ACCOUNT,
        region=REGION,
        environment_id="env-aws",
        clock=lambda: T0,
    )
    assert counts["created"] == 1
    for call in session.client.call_args_list:
        assert call.kwargs["config"].ignore_configured_endpoint_urls is True
        assert call.kwargs["config"].proxies == {}
        assert call.kwargs["region_name"] == REGION


def test_other_account_never_queries_hub():
    session = Mock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "111122223333"}
    counts = collect_securityhub(
        Store(),
        session,
        account_id=ACCOUNT,
        region=REGION,
        environment_id="env-aws",
        clock=lambda: T0,
    )
    assert counts == {"failed": 1}
    assert session.client.call_count == 1


def test_guardduty_registration_requires_explicit_handover():
    store, session = Store(), Mock()
    store.register_source("aws_guardduty", "env-aws")
    assert collect_securityhub(
        store,
        session,
        account_id=ACCOUNT,
        region=REGION,
        environment_id="env-aws",
        clock=lambda: T0,
    ) == {"failed": 1}
    session.client.assert_not_called()


def test_collection_deadline_cannot_advance_cursor(monkeypatch):
    client = fake()
    times = iter([0, 1, 2, 121])
    monkeypatch.setattr("bravo_findings.securityhub.time.monotonic", lambda: next(times))
    counts, store = run(client)
    assert counts == {"failed": 1}
    assert store.source("aws_security_hub", "env-aws").error_code == "TIMEOUT"
    assert store.source("aws_security_hub", "env-aws").cursor is None
    assert store.findings(environment_id="env-aws") == []


def test_401_after_hub_read_preserves_failure_without_cursor():
    client = fake()
    client.get_findings.side_effect = ClientError(
        {"Error": {"Code": "InvalidAccessException", "Message": "private"}}, "GetFindings"
    )
    counts, store = run(client)
    assert counts == {"failed": 1}
    row = store.source("aws_security_hub", "env-aws")
    assert row.error_code == "ACCESS_DENIED" and row.cursor is None
