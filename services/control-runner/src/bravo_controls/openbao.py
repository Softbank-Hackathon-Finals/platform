"""Read-only SEC-01.1 collector for registered OpenBao Kubernetes identities.

Registration/authentication belongs to the server, never uploaded JSON. Tokens
must come from independently verified Kubernetes logins. Only lookup-self and
one registered KV-v2 current-version read are available; no login/write API.
Secret-bearing responses stay in memory and must not be wire/debug logged.
"""

import hashlib
import http.client
import json
import math
import re
import ssl
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .runner import Environment, Observation, RunContext, TestSpec

TOOL = "bravo-controls-openbao"
VERSION = "1"
MAX_RESPONSE = 1024 * 1024
_SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}\Z")
_DNS = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*\Z")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or any(not _SEGMENT.fullmatch(p) or p.endswith(".") for p in value.split("/"))
    ):
        raise ValueError("registered API path required")
    return value


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_number(_):
    raise ValueError("non-JSON number")


class _NamedTLS(ssl.SSLContext):
    """Verifies the registered server name while the TCP connection goes to a loopback port-forward."""

    server_name = None

    def wrap_socket(self, sock, *args, **kwargs):
        if self.server_name is not None:
            kwargs["server_hostname"] = self.server_name
        return super().wrap_socket(sock, *args, **kwargs)


def tls_context(ca_pem: str, server_name: str | None = None) -> ssl.SSLContext:
    # Explicit trust bundle, no global SSL/environment trust override.
    context = _NamedTLS(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=ca_pem)
    context.server_name = server_name
    return context


def check_server_name(origin_host: str, server_name: str | None) -> None:
    """A registered server name only for a loopback origin (kubectl port-forward), and a DNS name."""
    if server_name is not None and (
        not isinstance(server_name, str) or not _DNS.fullmatch(server_name) or origin_host not in _LOOPBACK
    ):
        raise ValueError("server name only for a loopback origin")


@dataclass(frozen=True)
class KubernetesPrincipal:
    auth_mount: str
    namespace: str
    service_account: str
    uid: str
    role: str
    accessor: str = field(repr=False)

    def __post_init__(self):
        for value in (self.auth_mount, self.namespace, self.service_account, self.uid, self.role):
            if not isinstance(value, str) or not _SEGMENT.fullmatch(value):
                raise ValueError("registered Kubernetes identity required")
        if not isinstance(self.accessor, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,256}", self.accessor
        ):
            raise ValueError("registered token accessor required")


@dataclass(frozen=True)
class BaoTarget:
    app: str
    environment: Environment
    mount: str
    path: str
    version: int

    def __post_init__(self):
        if (
            not isinstance(self.app, str)
            or not _ID.fullmatch(self.app)
            or not isinstance(self.environment, Environment)
            or type(self.version) is not int
            or not 1 <= self.version <= 2**63 - 1
        ):
            raise ValueError("registered KV-v2 target required")
        _path(self.mount)
        _path(self.path)

    @property
    def read_path(self):
        # Read latest, then check returned version; old-version access is not current access.
        return f"/v1/{self.mount}/data/{self.path}"


@dataclass(frozen=True, repr=False)
class OpenBaoClient:
    origin: str
    ca_pem: str = field(repr=False)
    token: str = field(repr=False)
    principal: KubernetesPrincipal
    timeout: float = 3.0
    # Through `kubectl port-forward`: origin https://127.0.0.1:<port>, TLS checks this registered name.
    server_hostname: str | None = None

    def __post_init__(self):
        try:
            url = urlsplit(self.origin)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.path
                or url.query
                or url.fragment
                or not re.fullmatch(r"[A-Za-z0-9.:-]+", url.hostname)
                or (url.port is not None and not 1 <= url.port <= 65535)
                or not isinstance(self.principal, KubernetesPrincipal)
                or not isinstance(self.token, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,4096}", self.token)
                or type(self.timeout) not in (int, float)
                or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= 10
            ):
                raise ValueError
            check_server_name(url.hostname, self.server_hostname)
            self._tls()
        except Exception:
            raise ValueError(
                "registered HTTPS origin, CA, identity and bounded timeout required"
            ) from None

    def _tls(self):
        return tls_context(self.ca_pem, self.server_hostname)

    def _get(self, path: str) -> tuple[int, dict]:
        url = urlsplit(self.origin)
        connection = http.client.HTTPSConnection(
            url.hostname, url.port or 443, timeout=self.timeout, context=self._tls()
        )
        try:
            # No proxy environment, redirect following, retries, or cookies.
            connection.request(
                "GET", path, headers={"X-Vault-Token": self.token, "Accept": "application/json"}
            )
            response = connection.getresponse()
            if response.status not in (200, 403):
                return response.status, {}
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ValueError
            if (
                response.getheader("Content-Type", "").split(";", 1)[0].strip()
                != "application/json"
            ):
                raise ValueError
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError
            body = json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid_number)
            if not isinstance(body, dict):
                raise ValueError
            return response.status, body
        except TimeoutError:
            raise TimeoutError("OpenBao read timeout") from None
        except Exception:
            # Do not propagate URLs, credential headers, server errors or body values.
            raise ValueError("OpenBao read failed") from None
        finally:
            connection.close()

    def identity_matches(self) -> bool:
        status, body = self._get("/v1/auth/token/lookup-self")
        data = body.get("data")
        if status != 200 or not isinstance(data, dict):
            return False
        principal = self.principal
        meta = data.get("meta")
        policies = data.get("policies")
        return (
            data.get("accessor") == principal.accessor
            and data.get("path") == f"auth/{principal.auth_mount}/login"
            and type(data.get("ttl")) is int
            and data["ttl"] > 0
            and isinstance(policies, list)
            and bool(policies)
            and all(isinstance(p, str) for p in policies)
            and "root" not in policies
            and isinstance(meta, dict)
            and all(
                meta.get(k) == v
                for k, v in {
                    "role": principal.role,
                    "service_account_namespace": principal.namespace,
                    "service_account_name": principal.service_account,
                    "service_account_uid": principal.uid,
                }.items()
            )
        )

    def read_current(self, target: BaoTarget) -> dict:
        status, body = self._get(target.read_path)
        if status == 403 and body.get("errors") == ["permission denied"]:
            return {"outcome": "deny", "http_status": status, "reason": None}
        data = body.get("data")
        metadata = data.get("metadata") if isinstance(data, dict) else None
        valid = (
            status == 200
            and isinstance(metadata, dict)
            and type(metadata.get("version")) is int
            and metadata["version"] == target.version
            and metadata.get("destroyed") is False
            and metadata.get("deletion_time") == ""
            and isinstance(data.get("data"), dict)
        )
        return {
            "outcome": "allow" if valid else None,
            "http_status": status,
            "reason": None if valid else "UNEXPECTED_RESPONSE",
        }


class OpenBaoSecretAccessProbe:
    """Bracket one cross-SA denial with live identity and same-version app reads.

    This is a bounded observation, not an atomic policy snapshot. It does not
    prove ESO delivery, rotation, cluster registration or ongoing isolation.
    """

    def __init__(
        self,
        target: BaoTarget,
        *,
        app_client: OpenBaoClient,
        other_client: OpenBaoClient,
        evidence_sink: Callable[[bytes], None],
        clock=lambda: datetime.now(UTC),
    ):
        if not isinstance(target, BaoTarget) or any(
            type(c) is not OpenBaoClient for c in (app_client, other_client)
        ):
            raise ValueError("registered target and OpenBao clients required")
        if (
            app_client.origin != other_client.origin
            or app_client.server_hostname != other_client.server_hostname
            or app_client.ca_pem != other_client.ca_pem
            or app_client.token == other_client.token
            or app_client.principal.auth_mount != other_client.principal.auth_mount
            or (app_client.principal.namespace, app_client.principal.service_account)
            == (other_client.principal.namespace, other_client.principal.service_account)
            or app_client.principal.uid == other_client.principal.uid
            or app_client.principal.accessor == other_client.principal.accessor
            or not callable(evidence_sink)
            or not callable(clock)
        ):
            raise ValueError("same server and distinct registered service accounts required")
        self.target, self.app_client, self.other_client = target, app_client, other_client
        self._sink, self._clock = evidence_sink, clock

    @staticmethod
    def _identity(client):
        try:
            return {"matches": client.identity_matches(), "reason": None}
        except TimeoutError:
            return {"matches": False, "reason": "TIMEOUT"}
        except Exception:
            return {"matches": False, "reason": "TOOL_ERROR"}

    def _read(self, client):
        try:
            return client.read_current(self.target)
        except TimeoutError:
            return {"outcome": None, "http_status": None, "reason": "TIMEOUT"}
        except Exception:
            return {"outcome": None, "http_status": None, "reason": "TOOL_ERROR"}

    def __call__(self, context: RunContext, spec: TestSpec) -> Observation:
        if (
            not isinstance(context, RunContext)
            or not isinstance(context.run_id, str)
            or not _ID.fullmatch(context.run_id)
            or context.app != self.target.app
            or context.environment != self.target.environment
            or spec != TestSpec("SEC-01.1", "SEC-01", "1", "deny", "required")
        ):
            raise ValueError("collector context or method mismatch")
        steps = {}
        for name, client in (
            ("app_identity_before", self.app_client),
            ("other_identity_before", self.other_client),
        ):
            steps[name] = self._identity(client)
        positive = False
        observed = None
        reason = "OBSERVATION_DENIED"
        if all(s["matches"] for s in steps.values()):
            steps["app_read_before"] = self._read(self.app_client)
            positive = steps["app_read_before"]["outcome"] == "allow"
            if positive:
                steps["other_read"] = self._read(self.other_client)
                negative = steps["other_read"]
                # Recheck identity even on allow/error, so revoked/wrong principals never pass.
                for name, client in (
                    ("app_identity_after", self.app_client),
                    ("other_identity_after", self.other_client),
                ):
                    steps[name] = self._identity(client)
                steps["app_read_after"] = self._read(self.app_client)
                positive = steps["app_read_after"]["outcome"] == "allow"
                if (
                    all(steps[n]["matches"] for n in ("app_identity_after", "other_identity_after"))
                    and positive
                ):
                    observed, reason = negative["outcome"], negative["reason"]
                elif not positive:
                    reason = "POSITIVE_CONTROL_FAILED"
            else:
                reason = "POSITIVE_CONTROL_FAILED"
        collected = self._clock()
        if (
            not isinstance(collected, datetime)
            or collected.tzinfo is None
            or collected.utcoffset() is None
        ):
            raise ValueError("timezone-aware collector clock required")
        collected = collected.astimezone(UTC)
        payload = {
            "format_version": "1",
            "run_id": context.run_id,
            "app": context.app,
            "environment": asdict(context.environment),
            "test_id": "SEC-01.1",
            "method_version": "1",
            "tool": TOOL,
            "tool_version": VERSION,
            "collected_at": collected.isoformat(),
            "resource_binding_sha256": _digest(
                _canonical(
                    {"origin": self.app_client.origin, **asdict(self.target)}
                    | ({"server_hostname": self.app_client.server_hostname}
                       if self.app_client.server_hostname else {})
                )
            ),
            "app_principal_sha256": _digest(_canonical(asdict(self.app_client.principal))),
            "other_principal_sha256": _digest(_canonical(asdict(self.other_client.principal))),
            "steps": steps,
        }
        evidence = _canonical(payload)
        try:
            self._sink(evidence)
        except Exception:
            raise ValueError("collector evidence retention failed") from None
        return Observation(
            context,
            "SEC-01.1",
            "1",
            observed,
            "pass" if positive else "fail",
            collected,
            TOOL,
            VERSION,
            _digest(evidence),
            reason,
        )
