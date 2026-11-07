"""Real GCP REST adapter (gcp_rest) against fake openers and fake subprocess runners."""

import io
import json
import logging
import subprocess
import urllib.error

import pytest

from bravo_controls.gcp_probes import GcpApiError, GcpClient
from bravo_controls.gcp_rest import (
    POD_PROGRAM,
    GcpRestCall,
    PodExecTransport,
    TransportError,
    UrllibTransport,
    gcloud_token,
    rest_client,
)

TOKEN = "ya29.FAKE-TOKEN-must-never-appear-0123456789"
SA = "bravo-a11-app@fixture-proj.iam.gserviceaccount.invalid"
SECRET = "projects/fixture-proj/secrets/bravo-a11-ctl-db/versions/1"


class Response:
    def __init__(self, status, body):
        self.status, self._raw = status, json.dumps(body).encode()

    def read(self, _limit=-1):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def http_error(status, body):
    raw = io.BytesIO(json.dumps(body).encode())
    return urllib.error.HTTPError("https://example.invalid", status, "x", {}, raw)


def fake_gcloud(token=TOKEN):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=token + "\n", stderr="")

    run.calls = calls
    return run


def test_routes_methods_to_fixed_google_urls_with_bearer_token():
    opener = Opener(
        Response(200, {"bucket": "b-1", "name": "a/b c.txt", "generation": "7"}),
        Response(200, {"name": SECRET, "payload": {"data": "c2VjcmV0"}}),
        Response(200, {"entries": []}),
    )
    call = GcpRestCall(UrllibTransport(gcloud_token(run=fake_gcloud()), opener=opener))
    assert call("storage.objects.get", {"bucket": "b-1", "object": "a/b c.txt"})["generation"] == "7"
    access = call("secretmanager.versions.access", {"name": SECRET})
    assert access == {"name": SECRET}  # payload dropped before the probe sees it
    call("logging.entries.list", {"resourceNames": ["projects/fixture-proj"], "filter": "x"})
    urls = [r.full_url for r, _ in opener.requests]
    assert urls == [
        "https://storage.googleapis.com/storage/v1/b/b-1/o/a%2Fb%20c.txt",
        f"https://secretmanager.googleapis.com/v1/{SECRET}:access",
        "https://logging.googleapis.com/v2/entries:list",
    ]
    assert all(r.get_header("Authorization") == "Bearer " + TOKEN for r, _ in opener.requests)
    assert opener.requests[2][0].get_method() == "POST"
    assert all(timeout == 10 for _, timeout in opener.requests)


def test_anonymous_transport_sends_no_authorization_header():
    opener = Opener(http_error(401, {"error": {"code": 401, "message": "Anonymous caller"}}))
    client = rest_client("anonymous", "fixture-proj", UrllibTransport(None, opener=opener))
    with pytest.raises(GcpApiError) as raised:
        client.call("storage.objects.get", {"bucket": "b-1", "object": "o"})
    assert (raised.value.status, raised.value.code) == (401, "UNAUTHENTICATED")
    assert opener.requests[0][0].get_header("Authorization") is None


def test_anonymous_label_requires_credential_free_transport():
    with pytest.raises(ValueError):
        rest_client("anonymous", "fixture-proj", UrllibTransport(gcloud_token(run=fake_gcloud())))
    with pytest.raises(ValueError):
        rest_client(SA, "fixture-proj", UrllibTransport(None))


@pytest.mark.parametrize(
    "status, body, code",
    [
        (403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "m"}}, "PERMISSION_DENIED"),
        # GCS JSON API omits error.status → canonical HTTP mapping.
        (403, {"error": {"code": 403, "errors": [{"reason": "forbidden"}]}}, "PERMISSION_DENIED"),
        (404, {"error": {"code": 404, "status": "NOT_FOUND"}}, "NOT_FOUND"),
        (400, {"error": {"code": 400, "status": "FAILED_PRECONDITION"}}, "FAILED_PRECONDITION"),
        (418, "not json", "UNKNOWN"),
    ],
)
def test_errors_normalize_to_status_and_rpc_code_only(status, body, code):
    opener = Opener(http_error(status, body))
    call = GcpRestCall(UrllibTransport(gcloud_token(run=fake_gcloud()), opener=opener))
    with pytest.raises(GcpApiError) as raised:
        call("secretmanager.secrets.get", {"name": SECRET.rsplit("/versions/", 1)[0]})
    assert (raised.value.status, raised.value.code) == (status, code)
    assert str(raised.value) == f"GcpApiError({status})"


def test_timeouts_and_network_errors_carry_no_text():
    opener = Opener(TimeoutError("t"), urllib.error.URLError(TimeoutError()), urllib.error.URLError("dns secret-host"))
    call = GcpRestCall(UrllibTransport(None, opener=opener))
    req = {"bucket": "b-1", "object": "o"}
    with pytest.raises(TimeoutError):
        call("storage.objects.get", req)
    with pytest.raises(TimeoutError):
        call("storage.objects.get", req)
    with pytest.raises(TransportError) as raised:
        call("storage.objects.get", req)
    assert str(raised.value) == "GCP transport failed" and raised.value.__cause__ is None


@pytest.mark.parametrize(
    "method, request_",
    [
        ("storage.objects.get", {"bucket": "../evil", "object": "o"}),
        ("storage.objects.get", {"bucket": "b-1", "object": "o", "alt": "media"}),
        ("secretmanager.versions.access", {"name": "projects/p/secrets/x/versions/latest"}),
        ("secretmanager.versions.access", {"name": SECRET + "/../../x"}),
        ("cloudkms.cryptoKeyVersions.get", {"name": "https://evil.example/x"}),
        ("logging.entries.list", {"resourceNames": ["organizations/1"], "filter": "x"}),
        ("logging.entries.write", {"name": "x"}),
    ],
)
def test_unregistered_methods_and_fields_are_refused_before_any_request(method, request_):
    opener = Opener()
    with pytest.raises(ValueError):
        GcpRestCall(UrllibTransport(None, opener=opener))(method, request_)
    assert opener.requests == []


def test_token_never_reaches_logs_reprs_errors_or_argv(caplog):
    caplog.set_level(logging.DEBUG)
    run = fake_gcloud()
    source = gcloud_token(impersonate=SA, run=run)
    opener = Opener(
        http_error(403, {"error": {"status": "PERMISSION_DENIED", "message": "Bearer " + TOKEN}}),
        Response(200, {"name": SECRET, "payload": {"data": TOKEN}}),
    )
    client = rest_client(SA, "fixture-proj", UrllibTransport(source, opener=opener))
    texts = [repr(client), repr(client.call), repr(source)]
    with pytest.raises(GcpApiError) as raised:
        client.call("secretmanager.versions.access", {"name": SECRET})
    texts += [str(raised.value), repr(raised.value), repr(raised.value.args)]
    texts.append(json.dumps(client.call("secretmanager.versions.access", {"name": SECRET})))
    texts.append(caplog.text)
    assert all(TOKEN not in text for text in texts)
    # The token is read from gcloud stdout, never passed on a command line.
    argv, kwargs = run.calls[0]
    assert TOKEN not in " ".join(argv) and kwargs["capture_output"] is True
    assert argv[-1] == f"--impersonate-service-account={SA}"
    assert len(run.calls) == 1  # cached in memory for the TTL


def test_bad_gcloud_output_is_not_used_as_token():
    source = gcloud_token(run=fake_gcloud("ERROR: (gcloud) not logged in"))
    call = GcpRestCall(UrllibTransport(source, opener=Opener()))
    with pytest.raises(TransportError) as raised:
        call("storage.objects.get", {"bucket": "b-1", "object": "o"})
    assert "gcloud" not in str(raised.value)


def test_pod_exec_keeps_token_in_pod_and_sends_request_on_stdin():
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"status": 403, "body": {"error": {"code": 403, "status": "PERMISSION_DENIED"}}})
        )

    client = rest_client(SA, "fixture-proj", PodExecTransport("a11-probe", "intruder-0", run=run))
    with pytest.raises(GcpApiError) as raised:
        client.call("secretmanager.versions.access", {"name": SECRET})
    assert (raised.value.status, raised.value.code) == (403, "PERMISSION_DENIED")
    argv, kwargs = seen[0]
    assert argv[:6] == ["kubectl", "exec", "-i", "-n", "a11-probe", "intruder-0"]
    assert json.loads(kwargs["input"])["url"].endswith(":access")
    # The in-pod program drops payload and error messages before printing.
    assert 'body.pop("payload", None)' in POD_PROGRAM and '"message"' not in POD_PROGRAM


def test_pod_exec_failures_are_textless():
    def run(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, output="", stderr="pod secret-pod not found")

    call = GcpRestCall(PodExecTransport("a11-probe", "p", run=run))
    with pytest.raises(TransportError) as raised:
        call("storage.objects.get", {"bucket": "b-1", "object": "o"})
    assert raised.value.__cause__ is None and "secret-pod" not in repr(raised.value)


def test_rest_client_is_a_valid_gcp_client():
    client = rest_client(SA, "fixture-proj", UrllibTransport(gcloud_token(run=fake_gcloud())), timeout_seconds=5)
    assert isinstance(client, GcpClient) and client.timeout_seconds == 5
    with pytest.raises(ValueError):
        GcpRestCall(UrllibTransport(None), timeout_seconds=30)
