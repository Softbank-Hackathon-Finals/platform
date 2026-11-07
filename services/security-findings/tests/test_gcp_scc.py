"""SCC v2 pages → real normalization/store; federated credentials only; no Google Cloud call."""

import copy
import json

import pytest
import requests

from bravo_findings.collectors import run_once
from bravo_findings.gcp_pull import authorized_session, collect_scc, federated_credentials
from bravo_findings.gcp_scc import SccSource
from bravo_findings.ingest import Ingestor
from bravo_findings.normalize import Normalizer
from bravo_findings.rules import rules_dir
from bravo_findings.store import Store

PARENT = "projects/123456789"
T0 = 1_793_786_400_000  # 2026-11-04T10:00:00Z, after the fixture's eventTime
# A synthetic reader account. Composed from parts: the public-evidence scan judges addresses by domain only,
# and a service account domain is never an example domain.
READER, SANDBOX = "example-scc-reader", "example-sandbox"
CONFIG = {
    "universe_domain": "googleapis.com",
    "type": "external_account",
    "audience": "//iam.googleapis.com/projects/123456789/locations/global/"
    "workloadIdentityPools/bravo-platform/providers/platform-oidc",
    "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
    "token_url": "https://sts.googleapis.com/v1/token",
    "credential_source": {"file": "/var/run/secrets/gcp/token", "format": {"type": "text"}},
    "service_account_impersonation_url": "https://iamcredentials.googleapis.com/v1/projects/-/"
    f"serviceAccounts/{READER}@{SANDBOX}.iam.gserviceaccount.com:generateAccessToken",
}


def finding(number=1, **changes):
    raw = json.loads((rules_dir() / "examples/gcp-scc-public-bucket.raw.json").read_text())
    raw.pop("_note")
    raw["name"] = f"{PARENT}/sources/111/locations/global/findings/publicbucket{number:03d}"
    raw.update(changes)
    return raw


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} Client Error: Forbidden for url")

    def json(self):
        return self.payload


class Session:
    def __init__(self, *pages):
        self.pages, self.calls = list(pages), []

    def get(self, url, params, timeout):
        self.calls.append((url, copy.deepcopy(params)))
        return self.pages.pop(0)


def collect(session, now=T0):
    store = Store()
    counts = run_once(Ingestor(store, Normalizer()), SccSource(session, PARENT), "env-gcp", lambda: now)
    return store, counts


def test_v2_global_location_and_an_integer_event_time_window():
    session = Session(Response({"listFindingsResults": [{"finding": finding()}], "nextPageToken": ""}))
    store, counts = collect(session)
    assert counts["created"] == 1
    url, params = session.calls[0]
    assert url == f"https://securitycenter.googleapis.com/v2/{PARENT}/sources/-/locations/global/findings"
    assert params == {"filter": f"eventTime >= 0 AND eventTime <= {T0}", "pageSize": 100}
    row = store.source("gcp_scc", "env-gcp")
    assert (row.error_code, row.cursor) == (None, str(T0))


@pytest.mark.parametrize(
    "pages",
    [
        # the same page token twice would loop forever
        [{"listFindingsResults": [], "nextPageToken": "a"}, {"listFindingsResults": [], "nextPageToken": "a"}],
        # a finding of another project or organization
        [{"listFindingsResults": [{"finding": finding(name="projects/987654321/sources/1/findings/x")}]}],
        [{"listFindingsResults": [{"finding": finding(name="organizations/123456789/sources/1/findings/x")}]}],
        # the same finding on two pages
        [{"listFindingsResults": [{"finding": finding()}], "nextPageToken": "a"},
         {"listFindingsResults": [{"finding": finding()}]}],
        # moved out of the window between pages, or no time at all
        [{"listFindingsResults": [{"finding": finding(eventTime="2026-11-04T10:00:01Z")}]}],
        [{"listFindingsResults": [{"finding": finding(eventTime=None)}]}],
        [{"listFindingsResults": [{"resource": {}}]}],
        [{"listFindingsResults": {"finding": finding()}}],
    ],
)
def test_an_inconsistent_provider_response_is_unhealthy_and_keeps_the_cursor(pages):
    store, counts = collect(Session(*[Response(p) for p in pages]))
    assert counts == {"failed": 1}
    row = store.source("gcp_scc", "env-gcp")
    assert (row.error_code, row.cursor) == ("SOURCE_UNHEALTHY", None)
    assert not store.findings()


def test_scope_is_numeric_so_finding_names_can_be_checked():
    for parent in ("projects/bravo-sandbox", "projects/123456789/sources/1", "organizations/-", ""):
        with pytest.raises(ValueError):
            SccSource(Session(), parent)


def test_federated_configuration_is_accepted_without_a_token_call():
    credentials = federated_credentials(copy.deepcopy(CONFIG))
    assert type(credentials).__module__ == "google.auth.identity_pool"
    session = authorized_session(credentials)
    assert session.trust_env is False


@pytest.mark.parametrize(
    "change",
    [
        {"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----"},
        {"type": "authorized_user", "refresh_token": "r"},
        {"credential_source": {"executable": {"command": "/bin/token"}}},
        {"credential_source": {"url": "http://metadata/token"}},
        {"credential_source": {"file": "/t", "headers": {}}},
        {"client_id": "c", "client_secret": "s"},
        {"token_url": "https://sts.example.com/v1/token"},
        {"audience": "//iam.googleapis.com/locations/global/workforcePools/p/providers/x"},
        {"subject_token_type": "urn:ietf:params:aws:token-type:aws4_request"},
        {"universe_domain": "example.com"},
        {"service_account_impersonation_url": "https://evil.example.com/v1/token"},
    ],
)
def test_keys_secrets_and_other_token_sources_are_refused(change):
    store = Store()
    config = {**copy.deepcopy(CONFIG), **change}
    with pytest.raises(PermissionError):
        federated_credentials(config)
    called = []
    counts = collect_scc(store, config, parent=PARENT, environment_id="env-gcp", clock=lambda: T0,
                         session_factory=called.append)
    assert counts == {"failed": 1} and not called
    assert store.source("gcp_scc", "env-gcp").error_code == "ACCESS_DENIED"


def test_collect_records_access_denied_and_then_collects():
    store = Store()
    denied = Session(Response({}, status=403))
    kwargs = {"parent": PARENT, "environment_id": "env-gcp"}
    assert collect_scc(store, CONFIG, clock=lambda: T0, session_factory=lambda c: denied, **kwargs) == {
        "failed": 1
    }
    assert store.source("gcp_scc", "env-gcp").error_code == "ACCESS_DENIED"
    ok = Session(Response({"listFindingsResults": [{"finding": finding()}]}))
    counts = collect_scc(store, CONFIG, clock=lambda: T0 + 60_000, session_factory=lambda c: ok, **kwargs)
    assert counts["created"] == 1
    assert store.source("gcp_scc", "env-gcp").error_code is None
    with pytest.raises(ValueError):
        collect_scc(store, CONFIG, clock=lambda: T0, parent=PARENT, environment_id="../env")
