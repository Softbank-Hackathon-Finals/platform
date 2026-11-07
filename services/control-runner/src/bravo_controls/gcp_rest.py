"""Real ``GcpClient.call`` adapter: Google REST with short-lived access tokens (stdlib only).

``gcp_probes`` keeps every Google call behind ``GcpClient.call(method, request)``. This module
supplies the production side of that boundary without a new dependency:

* ``GcpRestCall`` maps the six registered methods to fixed Google REST URLs, validates every
  request field before it reaches a URL, strips Secret Manager payloads and normalizes failures
  to ``GcpApiError(status, code)`` (no message, header, URL or body text).
* A *transport* sends one request and returns ``(http_status, json_body)``. Two are provided:

  - ``UrllibTransport(token_source)``: ``urllib`` with a Bearer token from ``gcloud_token`` /
    ``metadata_token``, or no ``Authorization`` header at all for the anonymous IAM-01.1 client.
    Redirects are refused so a token is never replayed to another host.
  - ``PodExecTransport(namespace, pod)``: runs the request inside a GKE pod over
    ``kubectl exec`` so the pod's Workload Identity token never leaves the pod. The in-pod
    program removes ``payload`` before printing, so a secret value never leaves it either.

Tokens are never logged, stored in exceptions or reprs, written to disk or passed on a command
line. The adapter does not create credentials or service account keys.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from .gcp_probes import ANONYMOUS, GcpApiError, GcpClient

HOSTS = frozenset(
    {
        "storage.googleapis.com",
        "secretmanager.googleapis.com",
        "cloudkms.googleapis.com",
        "logging.googleapis.com",
    }
)
_BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]\Z")
_PROJECT = r"projects/[a-z0-9][a-z0-9-]{4,28}[a-z0-9]"
_SECRET = re.compile(_PROJECT + r"/secrets/[A-Za-z0-9_-]{1,255}\Z")
_SECRET_VERSION = re.compile(_PROJECT + r"/secrets/[A-Za-z0-9_-]{1,255}/versions/[0-9]{1,20}\Z")
_KMS_VERSION = re.compile(
    _PROJECT + r"/locations/[a-z0-9-]{1,63}/keyRings/[A-Za-z0-9_-]{1,63}"
    r"/cryptoKeys/[A-Za-z0-9_-]{1,63}/cryptoKeyVersions/[0-9]{1,20}\Z"
)
_RPC = re.compile(r"[A-Z][A-Z_]{2,40}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{20,4096}\Z")
_ENTRIES_KEYS = frozenset({"resourceNames", "filter", "orderBy", "pageSize", "pageToken"})
# google.rpc canonical HTTP mapping, used only when the body carries no ``error.status``
# (the GCS JSON API answers {"error": {"code": 403, "errors": [...]}} without it).
_HTTP_RPC = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    409: "ABORTED",
    429: "RESOURCE_EXHAUSTED",
    499: "CANCELLED",
    500: "INTERNAL",
    501: "NOT_IMPLEMENTED",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

Transport = Callable[[str, str, Mapping[str, Any] | None, float], tuple[int, Any]]


class TransportError(Exception):
    """Network or local process failure. Carries no URL, header, token or output text."""

    def __str__(self):
        return "GCP transport failed"


# --- token sources ---------------------------------------------------------------------------


class _CachedToken:
    """Holds one short-lived token in memory for ``ttl`` seconds. Never printed."""

    def __init__(self, fetch: Callable[[], str], ttl: float, monotonic=time.monotonic):
        self._fetch, self._ttl, self._monotonic = fetch, ttl, monotonic
        self._value: str | None = None
        self._expires = 0.0

    def __call__(self) -> str:
        now = self._monotonic()
        if self._value is None or now >= self._expires:
            value = self._fetch()
            if not isinstance(value, str) or not _TOKEN.fullmatch(value):
                raise TransportError() from None
            self._value, self._expires = value, now + self._ttl
        return self._value

    def __repr__(self):
        return "<token source>"


def gcloud_token(
    *, impersonate: str | None = None, run=subprocess.run, ttl: float = 300
) -> Callable[[], str]:
    """``gcloud auth print-access-token`` (optionally ``--impersonate-service-account``).

    The token is read from stdout into memory; stderr is discarded, never echoed.
    """
    argv = ["gcloud", "auth", "print-access-token", "--quiet"]
    if impersonate is not None:
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z0-9-]+\.iam\.gserviceaccount\.com", impersonate):
            raise ValueError("service account email required")
        argv.append(f"--impersonate-service-account={impersonate}")

    def fetch() -> str:
        try:
            done = run(argv, check=True, capture_output=True, text=True, timeout=30)
        except Exception:
            raise TransportError() from None
        return done.stdout.strip()

    return _CachedToken(fetch, ttl)


METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)


def metadata_token(*, opener=None, ttl: float = 300) -> Callable[[], str]:
    """GKE metadata server token (Workload Identity) for a server running in the pod."""
    opener = opener or urllib.request.build_opener(_NoRedirect())

    def fetch() -> str:
        request = urllib.request.Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
        try:
            with opener.open(request, timeout=5) as response:
                return json.loads(response.read(65536))["access_token"]
        except Exception:
            raise TransportError() from None

    return _CachedToken(fetch, ttl)


# --- transports ------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _decode(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return None


class UrllibTransport:
    """HTTPS to the fixed Google API hosts. ``token_source=None`` sends no credentials."""

    def __init__(self, token_source: Callable[[], str] | None, *, opener=None):
        self._token = token_source
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    @property
    def anonymous(self) -> bool:
        return self._token is None

    def __repr__(self):
        return f"UrllibTransport(anonymous={self.anonymous})"

    def __call__(self, http_method, url, body, timeout):
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self._token is not None:
            headers["Authorization"] = "Bearer " + self._token()
        request = urllib.request.Request(url, data=data, headers=headers, method=http_method)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return response.status, _decode(response.read(MAX_RESPONSE_BYTES))
        except urllib.error.HTTPError as error:
            try:
                raw = error.read(65536)
            except Exception:
                raw = b""
            finally:
                error.close()
            return error.code, _decode(raw)
        except TimeoutError:
            raise TimeoutError() from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TimeoutError() from None
            raise TransportError() from None
        except OSError:
            raise TransportError() from None


# Runs inside the pod with python3 only. Reads one request from stdin, gets the pod's
# Workload Identity token from the GKE metadata server, calls the fixed host and prints
# {"status": int, "body": ...} with any "payload" key removed. Never prints the token.
POD_PROGRAM = r"""
import json, sys, urllib.error, urllib.request
HOSTS = __HOSTS__
req = json.load(sys.stdin)
parts = req["url"].split("/")
if not req["url"].startswith("https://") or len(parts) < 4 or parts[2] not in HOSTS:
    print(json.dumps({"status": 0, "body": None})); sys.exit(0)
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
opener = urllib.request.build_opener(NoRedirect())
md = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
    headers={"Metadata-Flavor": "Google"})
token = json.loads(opener.open(md, timeout=5).read())["access_token"]
headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
data = None
if req.get("body") is not None:
    data = json.dumps(req["body"]).encode(); headers["Content-Type"] = "application/json"
r = urllib.request.Request(req["url"], data=data, headers=headers, method=req["method"])
try:
    resp = opener.open(r, timeout=req["timeout"]); status, raw = resp.status, resp.read(__MAX__)
except urllib.error.HTTPError as e:
    status, raw = e.code, e.read(65536)
try:
    body = json.loads(raw) if raw else {}
except ValueError:
    body = None
if isinstance(body, dict):
    body.pop("payload", None)
    if isinstance(body.get("error"), dict):
        body = {"error": {"code": body["error"].get("code"), "status": body["error"].get("status")}}
print(json.dumps({"status": status, "body": body}))
""".replace("__HOSTS__", repr(sorted(HOSTS))).replace("__MAX__", str(MAX_RESPONSE_BYTES))


class PodExecTransport:
    """Executes each request inside a GKE pod so its Workload Identity token stays there."""

    _NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z")

    def __init__(self, namespace: str, pod: str, *, container: str | None = None, run=subprocess.run):
        if not self._NAME.fullmatch(namespace) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", pod):
            raise ValueError("registered pod required")
        self.namespace, self.pod, self.container, self._run = namespace, pod, container, run

    def __repr__(self):
        return f"PodExecTransport({self.namespace}/{self.pod})"

    def __call__(self, http_method, url, body, timeout):
        argv = ["kubectl", "exec", "-i", "-n", self.namespace, self.pod]
        if self.container:
            argv += ["-c", self.container]
        argv += ["--", "python3", "-c", POD_PROGRAM]
        request = json.dumps({"method": http_method, "url": url, "body": body, "timeout": timeout})
        try:
            done = self._run(
                argv, input=request, check=True, capture_output=True, text=True, timeout=timeout + 20
            )
            reply = json.loads(done.stdout)
            status = reply["status"]
        except subprocess.TimeoutExpired:
            raise TimeoutError() from None
        except Exception:
            raise TransportError() from None
        if type(status) is not int or not 100 <= status <= 599:
            raise TransportError()
        return status, reply.get("body")


# --- method mapping --------------------------------------------------------------------------


def _need(value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("invalid registered GCP request")
    return value


def _route(method: str, request: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any] | None]:
    if not isinstance(request, Mapping):
        raise ValueError("invalid registered GCP request")
    if method == "storage.objects.get":
        bucket = _need(request.get("bucket"), _BUCKET)
        name = request.get("object")
        if (
            set(request) != {"bucket", "object"}
            or not isinstance(name, str)
            or not 1 <= len(name.encode()) <= 1024
            or any(ord(c) < 32 for c in name)
        ):
            raise ValueError("invalid registered GCP request")
        # Metadata GET (no alt=media): same storage.objects.get permission, no object bytes.
        quoted = urllib.parse.quote(name, safe="")
        return "GET", f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{quoted}", None
    if set(request) != {"name"} and method != "logging.entries.list":
        raise ValueError("invalid registered GCP request")
    if method == "secretmanager.versions.access":
        name = _need(request["name"], _SECRET_VERSION)
        return "GET", f"https://secretmanager.googleapis.com/v1/{name}:access", None
    if method == "secretmanager.versions.get":
        name = _need(request["name"], _SECRET_VERSION)
        return "GET", f"https://secretmanager.googleapis.com/v1/{name}", None
    if method == "secretmanager.secrets.get":
        name = _need(request["name"], _SECRET)
        return "GET", f"https://secretmanager.googleapis.com/v1/{name}", None
    if method == "cloudkms.cryptoKeyVersions.get":
        name = _need(request["name"], _KMS_VERSION)
        return "GET", f"https://cloudkms.googleapis.com/v1/{name}", None
    if method == "logging.entries.list":
        names = request.get("resourceNames")
        if (
            not set(request) <= _ENTRIES_KEYS
            or not isinstance(names, list)
            or len(names) != 1
            or not isinstance(names[0], str)
            or not re.fullmatch(_PROJECT + r"\Z", names[0])
            or not isinstance(request.get("filter"), str)
        ):
            raise ValueError("invalid registered GCP request")
        return "POST", "https://logging.googleapis.com/v2/entries:list", dict(request)
    raise ValueError("unregistered GCP method")


def _api_error(status: int, body: Any) -> GcpApiError:
    error = body.get("error") if isinstance(body, Mapping) else None
    code = error.get("status") if isinstance(error, Mapping) else None
    if not isinstance(code, str) or not _RPC.fullmatch(code):
        code = _HTTP_RPC.get(status, "UNKNOWN")
    return GcpApiError(status, code)


class GcpRestCall:
    """``GcpClient.call`` implementation over one transport (= one principal)."""

    def __init__(self, transport: Transport, *, timeout_seconds: float = 10):
        if not callable(transport) or not 0 < timeout_seconds <= 10:
            raise ValueError("bounded transport required")
        self._transport, self.timeout_seconds = transport, timeout_seconds

    def __repr__(self):
        return f"GcpRestCall({self._transport!r})"

    def __call__(self, method: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        http_method, url, body = _route(method, request)
        if urllib.parse.urlsplit(url).hostname not in HOSTS:
            raise ValueError("unregistered GCP host")
        status, payload = self._transport(http_method, url, body, self.timeout_seconds)
        if not 200 <= status <= 299:
            raise _api_error(status, payload)
        if not isinstance(payload, Mapping):
            raise GcpApiError(status, "INVALID_RESPONSE")
        result = dict(payload)
        # The probes never need the secret value; drop it before anything else sees it.
        result.pop("payload", None)
        return result


def rest_client(
    principal: str, project_id: str, transport: Transport, *, timeout_seconds: float = 10
) -> GcpClient:
    """Register one principal's client. ``principal`` is a label bound by the server; the
    anonymous label requires an ``UrllibTransport`` without a token source."""
    if (principal == ANONYMOUS) != (getattr(transport, "anonymous", False) is True):
        raise ValueError("anonymous label must match a credential-free transport")
    return GcpClient(
        principal,
        project_id,
        GcpRestCall(transport, timeout_seconds=timeout_seconds),
        timeout_seconds,
    )


__all__ = [
    "GcpRestCall",
    "PodExecTransport",
    "TransportError",
    "UrllibTransport",
    "gcloud_token",
    "metadata_token",
    "rest_client",
]
