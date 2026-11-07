"""gcp collectors against fake GcpClient.call implementations (no Google SDK, no network)."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bravo_controls import Catalog, Runner, default_registry
from bravo_controls.app_isolation import ClientObjectIsolationReader
from bravo_controls.audit_probes import AuditSearchProbe, CanaryReceipt
from bravo_controls.aws_probes import ObjectTarget
from bravo_controls.gcp_probes import (
    ENC_SPEC,
    CloudLoggingAuditSearch,
    CloudLoggingSecretLogSearch,
    GcpApiError,
    GcpClient,
    GcpSecretAccessProbe,
    GcpSecretEncryptionProbe,
    GcpSecretEncryptionTarget,
    GcpSecretTarget,
    GcsAnonymousReadProbe,
    gke_k8s_audit_event,
)
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec
from bravo_controls.secret_logs import SPEC as LOG_SPEC
from bravo_controls.secret_logs import LogCanaryReceipt, SecretLogProbe

ROOT = Path(__file__).resolve().parents[3]
PROJECT, NUMBER = "fixture-proj", "123456789"
WI = "principal://iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/fixture-proj.svc.id.goog/subject/ns/"
APP = WI + "demo-chat/sa/demo-chat"
OTHER = WI + "other-app/sa/other-app"
READER = WI + "bravo-controls/sa/control-reader"
ENV = Environment("gcp-target", "target", "gcp:gke/fixture", "gcp")
CTX = RunContext("run-1", "demo-chat", ENV)
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
SEC = Spec("SEC-01.1", "SEC-01", "1", "deny", "required")
IAM = Spec("IAM-01.1", "IAM-01", "1", "deny", "required")
SECRET = GcpSecretTarget("demo-chat", ENV, PROJECT, NUMBER, "chat-db", "3")
NAME = f"projects/{NUMBER}/secrets/chat-db/versions/3"
KEY = f"projects/{PROJECT}/locations/asia-northeast1/keyRings/bravo/cryptoKeys/secrets"
OBJECT = ObjectTarget(
    "demo-chat", ENV, PROJECT, "asia-northeast1", "chat-attach", "fixture/a.png", "1700000000",
    provider="gcs",
)
DENIED = GcpApiError(403, "PERMISSION_DENIED")


class NoPayload(dict):
    def __getitem__(self, key):
        assert key != "payload", "secret payload must never be read"
        return super().__getitem__(key)

    def get(self, key, default=None):
        assert key != "payload", "secret payload must never be read"
        return super().get(key, default)


class Fake:
    def __init__(self, **responses):
        self.responses, self.calls = responses, []

    def __call__(self, method, request):
        self.calls.append((method, dict(request)))
        value = self.responses[method.replace(".", "_")]
        value = value(request) if callable(value) else value
        if isinstance(value, Exception):
            raise value
        return value


def client(principal, **responses):
    return GcpClient(principal, PROJECT, Fake(**responses), 5)


def secret_probe(app, other, evidence=None):
    return GcpSecretAccessProbe(
        SECRET, app_client=app, other_client=other, app_principal=APP, other_principal=OTHER,
        evidence_sink=(evidence if evidence is not None else []).append, clock=lambda: NOW,
    )


ALLOW = NoPayload(name=NAME, payload={"data": "c2VjcmV0"})


def test_secret_other_principal_denied_passes_and_payload_is_never_read():
    evidence = []
    other = client(OTHER, secretmanager_versions_access=DENIED)
    result = secret_probe(client(APP, secretmanager_versions_access=ALLOW), other, evidence)(CTX, SEC)
    assert (result.observed, result.positive_control, result.unknown_reason) == ("deny", "pass", None)
    assert result.tool == "bravo-controls-gcp"
    assert "c2VjcmV0" not in evidence[0].decode()
    assert other.call.calls == [("secretmanager.versions.access", {"name": SECRET.name})]


@pytest.mark.parametrize(
    "negative, observed, reason",
    [
        (ALLOW, "allow", None),
        (GcpApiError(404, "NOT_FOUND"), None, "TOOL_ERROR"),
        (GcpApiError(401, "UNAUTHENTICATED"), None, "TOOL_ERROR"),
        (TimeoutError(), None, "TIMEOUT"),
    ],
)
def test_secret_only_permission_denied_counts_as_denial(negative, observed, reason):
    other = client(OTHER, secretmanager_versions_access=negative)
    result = secret_probe(client(APP, secretmanager_versions_access=ALLOW), other)(CTX, SEC)
    assert (result.observed, result.unknown_reason) == (observed, reason)


def test_secret_positive_control_failure_is_unknown():
    app = client(APP, secretmanager_versions_access=DENIED)
    result = secret_probe(app, client(OTHER, secretmanager_versions_access=DENIED))(CTX, SEC)
    assert (result.observed, result.unknown_reason) == (None, "POSITIVE_CONTROL_FAILED")


def test_secret_registration_requires_distinct_bound_principals():
    with pytest.raises(ValueError):
        secret_probe(client(APP), client(APP))
    with pytest.raises(ValueError):
        GcpSecretAccessProbe(
            SECRET, app_client=client(APP), other_client=client(OTHER),
            app_principal=APP, other_principal="arn:aws:iam::123456789012:role/x",
            evidence_sink=[].append,
        )
    with pytest.raises(ValueError):
        replace(SECRET, version="latest")


def gcs(principal, response):
    return client(principal, storage_objects_get=response)


META = {"bucket": "chat-attach", "name": "fixture/a.png", "generation": "1700000000"}


@pytest.mark.parametrize(
    "anonymous, observed, positive, reason",
    [
        (GcpApiError(401, "UNAUTHENTICATED"), "deny", "pass", None),
        (DENIED, "deny", "pass", None),
        (META, "allow", "pass", None),
        (GcpApiError(500, "INTERNAL"), None, "pass", "TOOL_ERROR"),
    ],
)
def test_gcs_anonymous_read(anonymous, observed, positive, reason):
    probe = GcsAnonymousReadProbe(
        OBJECT, app_client=gcs(APP, META), anonymous_client=gcs("anonymous", anonymous),
        app_principal_arn=APP, evidence_sink=[].append, clock=lambda: NOW,
    )
    result = probe(CTX, IAM)
    assert (result.observed, result.positive_control, result.unknown_reason) == (
        observed, positive, reason,
    )


def test_gcs_generation_drift_is_not_a_positive_control():
    drift = dict(META, generation="1700000001")
    probe = GcsAnonymousReadProbe(
        OBJECT, app_client=gcs(APP, drift), anonymous_client=gcs("anonymous", DENIED),
        app_principal_arn=APP, evidence_sink=[].append, clock=lambda: NOW,
    )
    assert probe(CTX, IAM).unknown_reason == "POSITIVE_CONTROL_FAILED"


def test_gcs_probe_refuses_unbound_clients_and_other_providers():
    with pytest.raises(ValueError):  # anonymous client actually carries a principal
        GcsAnonymousReadProbe(
            OBJECT, app_client=gcs(APP, META), anonymous_client=gcs(OTHER, DENIED),
            app_principal_arn=APP, evidence_sink=[].append,
        )
    with pytest.raises(ValueError):
        GcsAnonymousReadProbe(
            replace(OBJECT, provider="s3", account_id="123456789012", version_id="v1"),
            app_client=gcs(APP, META), anonymous_client=gcs("anonymous", DENIED),
            app_principal_arn=APP, evidence_sink=[].append,
        )


def test_registry_wires_gcp_collectors_into_a_complete_run():
    catalog = Catalog(
        json.loads((ROOT / "docs/controls/profiles.json").read_text()),
        json.loads((ROOT / "docs/controls/control-result.schema.json").read_text()),
    )
    wiring = {
        "SEC-01.1": dict(
            target=SECRET, app_client=client(APP, secretmanager_versions_access=ALLOW),
            other_client=client(OTHER, secretmanager_versions_access=DENIED),
            app_principal=APP, other_principal=OTHER, evidence_sink=[].append,
            clock=lambda: NOW,
        ),
        "IAM-01.1": dict(
            target=OBJECT, app_client=gcs(APP, META),
            anonymous_client=gcs("anonymous", GcpApiError(401, "UNAUTHENTICATED")),
            app_principal_arn=APP, evidence_sink=[].append, clock=lambda: NOW,
        ),
    }
    ids = ["IAM-01.1", "SEC-01.1", "AUD-01.1"]
    probes = default_registry().probes(ENV, ids, wiring, clock=lambda: NOW)
    run = Runner(catalog, probes, clock=lambda: NOW).run(
        profile_id="demo-gate", profile_version="1", app="demo-chat", environment=ENV
    )
    verdicts = {row["test_id"]: (row["verdict"], row["unknown_reason"]) for row in run["results"]}
    assert verdicts == {
        "IAM-01.1": ("PASS", None),
        "SEC-01.1": ("PASS", None),
        "AUD-01.1": ("UNKNOWN", "NOT_RUN"),
    }


def kms(secret=None, version=None, key_state="ENABLED"):
    secret = secret or {
        "name": f"projects/{NUMBER}/secrets/chat-db",
        "replication": {"automatic": {"customerManagedEncryption": {"kmsKeyName": KEY}}},
    }
    version = version or {
        "name": NAME,
        "state": "ENABLED",
        "customerManagedEncryption": {"kmsKeyVersionName": KEY + "/cryptoKeyVersions/1"},
    }
    return client(
        READER,
        secretmanager_secrets_get=secret,
        secretmanager_versions_get=version,
        cloudkms_cryptoKeyVersions_get={"state": key_state},
    )


def encryption(reader, evidence=None):
    return GcpSecretEncryptionProbe(
        GcpSecretEncryptionTarget(SECRET, (KEY,)), reader_client=reader, reader_principal=READER,
        evidence_sink=(evidence if evidence is not None else []).append, clock=lambda: NOW,
    )


def test_cmek_with_enabled_key_version_is_enabled():
    evidence = []
    result = encryption(kms(), evidence)(CTX, ENC_SPEC)
    assert (result.observed, result.unknown_reason) == ("enabled", None)
    assert json.loads(evidence[0])["key_version_state"] == "ENABLED"


def test_google_default_encryption_or_disabled_key_is_disabled():
    plain = {"name": "x", "replication": {"automatic": {}}}
    version = {"name": NAME, "state": "ENABLED"}
    assert encryption(kms(plain, version))(CTX, ENC_SPEC).observed == "disabled"
    assert encryption(kms(key_state="DISABLED"))(CTX, ENC_SPEC).observed == "disabled"


def test_unregistered_key_is_unexpected_and_denied_read_is_permission_error():
    other = KEY.replace("secrets", "other")
    secret = {"name": "x", "replication": {"automatic": {"customerManagedEncryption": {"kmsKeyName": other}}}}
    result = encryption(kms(secret))(CTX, ENC_SPEC)
    assert (result.observed, result.unknown_reason) == (None, "UNEXPECTED_RESPONSE")
    with pytest.raises(PermissionError):
        encryption(client(READER, secretmanager_secrets_get=DENIED))(CTX, ENC_SPEC)


START = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)
MARKER = "bravo_audit_" + "d" * 32
ACTOR = "system:serviceaccount:bravo-system:platform-admin"
AUDIT_LOG = "cloudaudit.googleapis.com%2Factivity"


def rbac_entry(verb, at, insert, **proto):
    return {
        "insertId": insert,
        "logName": f"projects/{PROJECT}/logs/{AUDIT_LOG}",
        "timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.") + "123456789Z",
        "receiveTimestamp": (at + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "protoPayload": {
            "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
            "serviceName": "k8s.io",
            "methodName": f"io.k8s.authorization.rbac.v1.rolebindings.{verb}",
            "resourceName": f"rbac.authorization.k8s.io/v1/namespaces/demo-chat/rolebindings/{MARKER}",
            "authenticationInfo": {"principalEmail": ACTOR},
            **proto,
        },
    }


def audit_receipt():
    return CanaryReceipt(
        CTX, "AUD-01.3", MARKER, ACTOR, "chat", "demo-chat", "bravo_test",
        START, START + timedelta(seconds=5), START + timedelta(minutes=10),
        frozenset({"create", "delete"}),
    )


def audit_probe(pages):
    fake = Fake(logging_entries_list=lambda request: pages[request.get("pageToken", "")])
    search = CloudLoggingAuditSearch(
        GcpClient(READER, PROJECT, fake, 5), project_id=PROJECT, reader_principal=READER,
        log_ids=(AUDIT_LOG,), clock=lambda: START + timedelta(minutes=10),
    )
    probe = AuditSearchProbe(
        receipt_provider=lambda *_: audit_receipt(), search=search,
        source_binding=search.source_binding, streams=search.streams,
        evidence_sink=[].append, clock=lambda: START + timedelta(minutes=10),
    )
    return probe, fake


AUD3 = Spec("AUD-01.3", "AUD-01", "1", "present", "not_applicable")


def test_gke_cloud_audit_logs_rbac_change_is_found_across_pages():
    pages = {
        "": {"entries": [rbac_entry("create", START + timedelta(seconds=1), "a")], "nextPageToken": "p2"},
        "p2": {"entries": [rbac_entry("delete", START + timedelta(seconds=3), "b")]},
    }
    probe, fake = audit_probe(pages)
    result = probe(CTX, AUD3)
    assert (result.observed, result.unknown_reason) == ("present", None)
    query = fake.calls[0][1]
    assert MARKER in query["filter"] and AUDIT_LOG in query["filter"]
    assert query["resourceNames"] == [f"projects/{PROJECT}"]


def test_failed_or_impersonated_rbac_change_is_absent():
    entries = [
        rbac_entry("create", START + timedelta(seconds=1), "a", status={"code": 7}),
        rbac_entry(
            "delete", START + timedelta(seconds=3), "b",
            authenticationInfo={"principalEmail": ACTOR, "serviceAccountDelegationInfo": [{}]},
        ),
    ]
    probe, _ = audit_probe({"": {"entries": entries}})
    assert probe(CTX, AUD3).observed == "absent"


def test_audit_search_denied_or_bad_pagination_is_not_absence():
    probe, _ = audit_probe({"": DENIED})
    with pytest.raises(PermissionError):
        probe(CTX, AUD3)
    probe, _ = audit_probe({"": {"entries": [], "nextPageToken": "p"}, "p": {"entries": [], "nextPageToken": "p"}})
    with pytest.raises(ValueError):
        probe(CTX, AUD3)


def test_non_rbac_entries_are_not_projected():
    entry = rbac_entry("create", START, "a")
    entry["protoPayload"]["methodName"] = "io.k8s.core.v1.pods.create"
    assert gke_k8s_audit_event(entry) is None


SECRET_TEXT = "bravo_secret_" + "e" * 64
LOG_MARKER = "bravo_log_" + "f" * 32
END, SETTLED = START + timedelta(seconds=5), START + timedelta(seconds=10)
FILTER = 'resource.type="k8s_container" AND resource.labels.namespace_name="demo-chat"'


def log_entry(stream, insert, text):
    return {
        "insertId": insert,
        "logName": f"projects/{PROJECT}/logs/{stream}",
        "timestamp": (START + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "receiveTimestamp": (START + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "textPayload": text,
    }


def log_probe(entries):
    fake = Fake(logging_entries_list={"entries": entries})
    search = CloudLoggingSecretLogSearch(
        GcpClient(READER, PROJECT, fake, 5), project_id=PROJECT, reader_principal=READER,
        log_ids=("stdout", "stderr"), resource_filter=FILTER, clock=lambda: SETTLED,
    )
    receipt = LogCanaryReceipt(
        CTX, search.source_binding, search.streams, "sha256:" + "c" * 64, SECRET_TEXT, LOG_MARKER,
        START, END, SETTLED, frozenset({"configured", "exercised", "restored", "logs_drained"}),
    )
    probe = SecretLogProbe(
        receipt_provider=lambda *_: receipt, search=search, source_binding=search.source_binding,
        streams=search.streams, configuration_sha256=receipt.configuration_sha256,
        evidence_sink=[].append, clock=lambda: SETTLED,
    )
    return probe, fake


def test_cloud_logging_canary_window_without_secret_is_absent():
    probe, fake = log_probe([log_entry("stdout", "1", LOG_MARKER), log_entry("stderr", "2", LOG_MARKER)])
    result = probe(CTX, LOG_SPEC)
    assert (result.observed, result.positive_control) == ("absent", "pass")
    query = fake.calls[0][1]["filter"]
    assert FILTER in query and LOG_MARKER not in query and SECRET_TEXT not in query


def test_cloud_logging_secret_leak_is_present_and_missing_stream_is_unknown():
    leak = [log_entry("stdout", "1", LOG_MARKER + " " + SECRET_TEXT), log_entry("stderr", "2", LOG_MARKER)]
    assert log_probe(leak)[0](CTX, LOG_SPEC).observed == "present"
    result = log_probe([log_entry("stdout", "1", LOG_MARKER)])[0](CTX, LOG_SPEC)
    assert result.unknown_reason == "POSITIVE_CONTROL_FAILED"


def isolation(owner_response, other_response, cross):
    other_target = replace(OBJECT, app="other-app", bucket="other-attach")

    def other_get(request):
        return cross if request["bucket"] == OBJECT.bucket else other_response

    holder = {}
    reader = ClientObjectIsolationReader(
        OBJECT, other_target,
        owner_client=client(APP, storage_objects_get=owner_response),
        other_client=client(OTHER, storage_objects_get=other_get),
        owner_principal=APP, other_principal=OTHER,
        read_registration=lambda _: holder["binding"], clock=lambda: NOW,
    )
    holder["binding"] = reader.binding
    return reader


OTHER_META = {"bucket": "other-attach", "name": "fixture/a.png", "generation": "1700000000"}


def test_workload_identity_isolation_leg_for_gcs():
    leg = isolation(META, OTHER_META, DENIED)(CTX, "nonce-1")
    assert (leg.positive, leg.outcome, leg.reason) == (True, "deny", None)
    leg = isolation(META, OTHER_META, META)(CTX, "nonce-1")
    assert leg.outcome == "allow"
    leg = isolation(DENIED, OTHER_META, DENIED)(CTX, "nonce-1")
    assert (leg.outcome, leg.reason) == (None, "POSITIVE_CONTROL_FAILED")
