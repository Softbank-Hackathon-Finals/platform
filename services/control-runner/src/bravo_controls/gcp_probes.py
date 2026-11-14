"""Read-only GCP collectors (ADR-0034 ``both.control_probes`` for kind ``gcp``).

Every Google API call goes through a server-registered ``GcpClient.call`` so tests use
fakes and production can use google-cloud libraries or REST (AuthorizedSession) without
this module importing either. The adapter MUST:

* bind one verified principal (GKE Workload Identity GSA or KSA principal) per client,
  or ``anonymous`` for an unauthenticated client, and never switch it after registration;
* map an HTTP error to ``GcpApiError(status, code)`` where ``code`` is the google.rpc
  status name (``PERMISSION_DENIED``, ``UNAUTHENTICATED``, ``NOT_FOUND``...) and never
  copy messages, headers or URLs into it;
* raise ``TimeoutError`` for connect/read timeouts and disable request/response logging.

Like the AWS collectors, these never create credentials and never read secret payloads
or object contents. Principal labels are not identity proof.
"""

import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .audit_probes import TOKEN, AuditBatch, AuditEvent, CanaryReceipt
from .aws_probes import (
    _ID,
    S3AnonymousReadProbe,
    _canonical,
    _digest,
    _ReadProbe,
)
from .runner import Environment, Observation, RunContext, TestSpec
from .secret_logs import MAX_BYTES as LOG_MAX_BYTES
from .secret_logs import MAX_EVENTS as LOG_MAX_EVENTS
from .secret_logs import LogCanaryReceipt, LogWindow

TOOL = "bravo-controls-gcp"
TOOL_VERSION = "1"
PROJECT = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
PROJECT_NUMBER = re.compile(r"[0-9]{1,20}\Z")
_SA = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com\Z")
_WI = re.compile(
    r"principal://iam\.googleapis\.com/projects/[0-9]{1,20}/locations/global/"
    r"workloadIdentityPools/[a-z][a-z0-9-]{4,28}[a-z0-9]\.svc\.id\.goog/subject/ns/"
    r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?/sa/[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?\Z"
)
_SECRET_ID = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")
_KMS_KEY = re.compile(
    r"projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/locations/[a-z0-9-]{1,63}/keyRings/"
    r"[A-Za-z0-9_-]{1,63}/cryptoKeys/[A-Za-z0-9_-]{1,63}\Z"
)
_LOG_ID = re.compile(r"[A-Za-z0-9._%/-]{1,512}\Z")
_TIME = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z\Z")
ANONYMOUS = "anonymous"
MAX_PAGES = 10
BUDGET_SECONDS = 30


class GcpApiError(Exception):
    """Normalized Google API failure. Carries no message, header or URL text."""

    def __init__(self, status: int, code: str):
        super().__init__()
        self.status = status
        self.code = code

    def __str__(self):
        return f"GcpApiError({self.status})"


@dataclass(frozen=True, eq=False, repr=False)
class GcpClient:
    """Server-registered call boundary for one principal in one project."""

    principal: str
    project_id: str
    call: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] = field(repr=False)
    timeout_seconds: float = 10


def _gcp_principal(value: str) -> None:
    if not isinstance(value, str) or not (_SA.fullmatch(value) or _WI.fullmatch(value)):
        raise ValueError("registered GCP principal required")


def _gcp_client(
    client: Any, project_id: str, *, anonymous: bool = False, principal: str | None = None
) -> None:
    try:
        timeout = client.timeout_seconds
        if (
            not isinstance(client, GcpClient)
            or client.project_id != project_id
            or not callable(client.call)
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 10
            or (client.principal == ANONYMOUS) != anonymous
            or (principal is not None and client.principal != principal)
        ):
            raise ValueError
        if not anonymous:
            _gcp_principal(client.principal)
    except Exception:
        raise ValueError("bounded server-registered GCP client required") from None


def _gcp_failure(error: Exception, *, anonymous: bool) -> dict:
    if isinstance(error, GcpApiError):
        pair = (error.status, error.code)
        denied = pair == (403, "PERMISSION_DENIED") or (
            anonymous and pair == (401, "UNAUTHENTICATED")
        )
        status = error.status if type(error.status) is int and 100 <= error.status <= 599 else None
        if denied:
            return {"outcome": "deny", "http_status": status, "reason": None}
        return {"outcome": None, "http_status": status, "reason": "TOOL_ERROR"}
    reason = "TIMEOUT" if isinstance(error, TimeoutError) else "TOOL_ERROR"
    return {"outcome": None, "http_status": None, "reason": reason}


def _gcs_read(target, client) -> tuple[str | None, int | None, str | None]:
    """Object metadata GET needs storage.objects.get, the same permission as a download."""
    try:
        response = client.call(
            "storage.objects.get", {"bucket": target.bucket, "object": target.key}
        )
    except Exception as error:
        result = _gcp_failure(error, anonymous=client.principal == ANONYMOUS)
        return result["outcome"], result["http_status"], result["reason"]
    valid = (
        isinstance(response, Mapping)
        and response.get("bucket") == target.bucket
        and response.get("name") == target.key
        and response.get("generation") == target.version_id
    )
    return ("allow", 200, None) if valid else (None, None, "UNEXPECTED_RESPONSE")


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware collector clock required")
    return value.astimezone(UTC)


def _rfc3339(value: Any) -> datetime:
    match = _TIME.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("RFC 3339 UTC timestamp required")
    micros = int((match[2] or "0")[:6].ljust(6, "0"))
    return datetime.fromisoformat(match[1] + "+00:00").replace(microsecond=micros)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class GcpSecretTarget:
    app: str
    environment: Environment
    project_id: str
    project_number: str
    secret_id: str
    version: str

    def __post_init__(self):
        if (
            not isinstance(self.app, str)
            or not _ID.fullmatch(self.app)
            or not isinstance(self.environment, Environment)
            or not isinstance(self.project_id, str)
            or not PROJECT.fullmatch(self.project_id)
            or not isinstance(self.project_number, str)
            or not PROJECT_NUMBER.fullmatch(self.project_number)
            or not isinstance(self.secret_id, str)
            or not _SECRET_ID.fullmatch(self.secret_id)
            or not isinstance(self.version, str)
            # A pinned version number; "latest" could move during the bracketed reads.
            or not re.fullmatch(r"[1-9][0-9]{0,19}", self.version)
        ):
            raise ValueError("invalid registered GCP Secret target")

    @property
    def name(self) -> str:
        return f"projects/{self.project_id}/secrets/{self.secret_id}/versions/{self.version}"

    def names(self) -> frozenset[str]:
        # Secret Manager answers with the project number in resource names.
        suffix = f"/secrets/{self.secret_id}/versions/{self.version}"
        return frozenset(
            {f"projects/{self.project_id}{suffix}", f"projects/{self.project_number}{suffix}"}
        )


class GcpSecretAccessProbe(_ReadProbe):
    """SEC-01.1 gcp: app principal reads, another Workload Identity principal is denied.

    Denial is accepted only as HTTP 403 ``PERMISSION_DENIED``; a later app read
    brackets it because Secret Manager also answers 403 for an invisible secret.
    """

    test_id = "SEC-01.1"
    action = "secretmanager.versions.access"
    _recheck_denial = True
    tool = TOOL
    tool_version = TOOL_VERSION

    def __init__(
        self,
        target: GcpSecretTarget,
        *,
        app_client,
        other_client,
        app_principal: str,
        other_principal: str,
        evidence_sink: Callable[[bytes], None],
        clock=lambda: datetime.now(UTC),
    ):
        if not isinstance(target, GcpSecretTarget):
            raise ValueError("registered GCP Secret target required")
        _gcp_principal(app_principal)
        _gcp_principal(other_principal)
        if app_principal == other_principal:
            raise ValueError("different registered GCP principals required")
        self._setup(
            target, app_client, other_client, app_principal, other_principal, evidence_sink, clock
        )
        self._validate_clients()

    def _validate_clients(self):
        _gcp_client(self.app_client, self.target.project_id, principal=self._app_principal)
        _gcp_client(
            self.negative_client, self.target.project_id, principal=self._negative_principal
        )

    def _resource_binding(self):
        return {"secret_version": self.target.name, "project_number": self.target.project_number}

    def _read(self, client):
        try:
            response = client.call("secretmanager.versions.access", {"name": self.target.name})
        except Exception as error:
            return _gcp_failure(error, anonymous=False)
        # Deliberately never access response["payload"], including a negative success.
        valid = isinstance(response, Mapping) and response.get("name") in self.target.names()
        return {
            "outcome": "allow" if valid else None,
            "http_status": 200 if valid else None,
            "reason": None if valid else "UNEXPECTED_RESPONSE",
        }


class GcsAnonymousReadProbe(S3AnonymousReadProbe):
    """IAM-01.1 gcp: unauthenticated GCS object GET is denied (401/403).

    The app principal is a GKE Workload Identity principal; ``anonymous_client`` must
    send no credentials. The returned generation must equal the registered one.
    """

    action = "storage.objects.get"
    providers = ("gcs",)
    tool = TOOL
    tool_version = TOOL_VERSION


ENC_SPEC = TestSpec("ENC-02.1", "ENC-02", "1", "enabled", "not_applicable")


@dataclass(frozen=True)
class GcpSecretEncryptionTarget:
    secret: GcpSecretTarget
    # One Cloud KMS key per replica location (automatic replication uses exactly one).
    kms_key_names: tuple[str, ...]

    def __post_init__(self):
        keys = self.kms_key_names
        if (
            not isinstance(self.secret, GcpSecretTarget)
            or not isinstance(keys, tuple)
            or not 1 <= len(keys) <= 20
            or len(set(keys)) != len(keys)
            or any(not isinstance(k, str) or not _KMS_KEY.fullmatch(k) for k in keys)
        ):
            raise ValueError("invalid registered Cloud KMS binding")


def _cmek_keys(secret: Mapping) -> frozenset | None:
    replication = secret.get("replication")
    if not isinstance(replication, Mapping) or len(replication) != 1:
        raise ValueError("unexpected Secret replication")
    if "automatic" in replication:
        configs = [replication["automatic"]]
    elif "userManaged" in replication:
        replicas = replication["userManaged"].get("replicas")
        if not isinstance(replicas, list) or not replicas:
            raise ValueError("unexpected Secret replicas")
        configs = replicas
    else:
        raise ValueError("unexpected Secret replication")
    keys = []
    for config in configs:
        if not isinstance(config, Mapping):
            raise ValueError("unexpected Secret replication")
        cmek = config.get("customerManagedEncryption")
        keys.append(cmek.get("kmsKeyName") if isinstance(cmek, Mapping) else None)
    if all(k is None for k in keys):
        return None
    return frozenset(keys)


def _version_key_versions(version: Mapping) -> list | None:
    """Cloud KMS key versions that encrypted this Secret version, one per replica.

    Global secrets report them in ``replicationStatus`` (``automatic`` or
    ``userManaged.replicas[]``); only regional secrets fill the top-level
    ``customerManagedEncryption``. ``None`` means no CMEK status anywhere.
    """
    top = version.get("customerManagedEncryption")
    if isinstance(top, Mapping):
        return [top.get("kmsKeyVersionName")]
    status = version.get("replicationStatus")
    if status is None:
        return None
    if not isinstance(status, Mapping) or len(status) != 1:
        raise ValueError("unexpected Secret version replication status")
    if "automatic" in status:
        configs = [status["automatic"]]
    elif "userManaged" in status and isinstance(status["userManaged"], Mapping):
        configs = status["userManaged"].get("replicas")
        if not isinstance(configs, list) or not configs:
            raise ValueError("unexpected Secret version replicas")
    else:
        raise ValueError("unexpected Secret version replication status")
    found = []
    for config in configs:
        if not isinstance(config, Mapping):
            raise ValueError("unexpected Secret version replication status")
        cmek = config.get("customerManagedEncryption")
        found.append(cmek.get("kmsKeyVersionName") if isinstance(cmek, Mapping) else None)
    return None if all(k is None for k in found) else found


class GcpSecretEncryptionProbe:
    """ENC-02.1 gcp: the pinned Secret version is encrypted with the registered Cloud KMS key.

    ``enabled``: every replica uses a registered key, every replica's key version (global
    secrets: ``replicationStatus``; regional: top-level) belongs to one of them and is
    ENABLED. ``disabled``: no CMEK at all (Google-default encryption is not the registered
    control) or a key version is disabled/destroyed. Any other key, a missing replica key
    version or a change between the bracketing reads is UNEXPECTED_RESPONSE.
    """

    def __init__(
        self,
        target: GcpSecretEncryptionTarget,
        *,
        reader_client,
        reader_principal: str,
        evidence_sink,
        clock=lambda: datetime.now(UTC),
    ):
        if not isinstance(target, GcpSecretEncryptionTarget) or not all(
            callable(x) for x in (evidence_sink, clock)
        ):
            raise ValueError("registered Cloud KMS target and evidence sink required")
        _gcp_principal(reader_principal)
        self.target, self.client, self.principal = target, reader_client, reader_principal
        self.sink, self.clock = evidence_sink, clock
        self._validate()

    def _validate(self):
        _gcp_client(self.client, self.target.secret.project_id, principal=self.principal)

    def _get(self, method, name):
        try:
            response = self.client.call(method, {"name": name})
        except GcpApiError as error:
            if (error.status, error.code) == (403, "PERMISSION_DENIED"):
                raise PermissionError("metadata read denied") from None
            raise ValueError("metadata read failed") from None
        if not isinstance(response, Mapping):
            raise ValueError("invalid metadata response")
        return response

    def __call__(self, context: RunContext, spec: TestSpec) -> Observation:
        secret_target = self.target.secret
        if (
            spec != ENC_SPEC
            or not isinstance(context, RunContext)
            or context.app != secret_target.app
            or context.environment != secret_target.environment
        ):
            raise ValueError("encryption method or registered target mismatch")
        self._validate()
        secret_name = secret_target.name.rsplit("/versions/", 1)[0]
        before = self._get("secretmanager.secrets.get", secret_name)
        version = self._get("secretmanager.versions.get", secret_target.name)
        keys = _cmek_keys(before)
        key_versions = _version_key_versions(version)
        registered = frozenset(self.target.kms_key_names)
        states: list = []
        if version.get("name") not in secret_target.names() or version.get("state") != "ENABLED":
            observed, reason = None, "UNEXPECTED_RESPONSE"
        elif keys is None and key_versions is None:
            observed, reason = "disabled", None
        elif (
            keys is not None
            and keys <= registered
            and key_versions is not None
            and all(isinstance(kv, str) for kv in key_versions)
            and all(any(kv.startswith(k + "/cryptoKeyVersions/") for k in keys) for kv in key_versions)
        ):
            states = [
                self._get("cloudkms.cryptoKeyVersions.get", kv).get("state")
                for kv in sorted(set(key_versions))
            ]
            if all(state == "ENABLED" for state in states):
                observed, reason = "enabled", None
            elif all(
                state in {"ENABLED", "DISABLED", "DESTROYED", "DESTROY_SCHEDULED"}
                for state in states
            ):
                observed, reason = "disabled", None
            else:
                observed, reason = None, "UNEXPECTED_RESPONSE"
        else:
            observed, reason = None, "UNEXPECTED_RESPONSE"
        if self._get("secretmanager.secrets.get", secret_name) != before:
            observed, reason = None, "UNEXPECTED_RESPONSE"
        collected = _utc(self.clock())
        payload = _canonical(
            {
                # 2: one key version per replica, read from replicationStatus (A11).
                "format_version": "2",
                "run_id": context.run_id,
                "app": context.app,
                "environment": context.environment.ref,
                "test_id": spec.test_id,
                "method_version": spec.method_version,
                "secret_version": secret_target.name,
                "registered_keys": sorted(registered),
                "observed_keys": sorted(k or "" for k in keys) if keys else [],
                "key_version_sha256": sorted(
                    _digest(kv.encode()) for kv in set(key_versions or ()) if isinstance(kv, str)
                ),
                "key_version_states": states,
                "reader_principal_sha256": _digest(self.principal.encode()),
                "collected_at": collected.isoformat(),
                "observed": observed,
                "unknown_reason": reason,
            }
        )
        self.sink(payload)
        return Observation(
            context,
            spec.test_id,
            spec.method_version,
            observed,
            "not_applicable",
            collected,
            TOOL,
            TOOL_VERSION,
            _digest(payload),
            reason,
        )


_RBAC_METHOD = re.compile(r"io\.k8s\.authorization\.rbac\.v1\.rolebindings\.(create|delete)\Z")
_RBAC_RESOURCE = re.compile(
    r"rbac\.authorization\.k8s\.io/v1/namespaces/([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)"
    r"/rolebindings/([A-Za-z0-9._-]{1,253})\Z"
)
_GRPC_HTTP = {0: 200, 3: 400, 5: 404, 6: 409, 7: 403, 16: 401}


def gke_k8s_audit_event(entry: Mapping) -> dict | None:
    """Project a GKE Cloud Audit Logs RoleBinding entry onto the audit.k8s.io/v1 fields
    ``audit_probes._matched`` reads. Not an RBAC entry → None (not a match)."""
    proto = entry.get("protoPayload")
    if (
        not isinstance(proto, Mapping)
        or proto.get("@type") != "type.googleapis.com/google.cloud.audit.AuditLog"
        or proto.get("serviceName") != "k8s.io"
    ):
        return None
    method = proto.get("methodName")
    resource = proto.get("resourceName")
    verb = _RBAC_METHOD.fullmatch(method) if isinstance(method, str) else None
    ref = _RBAC_RESOURCE.fullmatch(resource) if isinstance(resource, str) else None
    if not verb or not ref:
        return None
    auth = proto.get("authenticationInfo")
    status = proto.get("status")
    auth = auth if isinstance(auth, Mapping) else {}
    code = status.get("code", 0) if isinstance(status, Mapping) else 0
    return {
        "apiVersion": "audit.k8s.io/v1",
        "kind": "Event",
        "stage": "ResponseComplete",
        "verb": verb[1],
        "auditID": entry.get("insertId"),
        "user": {"username": auth.get("principalEmail")},
        "impersonatedUser": auth.get("serviceAccountDelegationInfo") or None,
        "objectRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "resource": "rolebindings",
            "namespace": ref[1],
            "name": ref[3],
        },
        "responseStatus": {"code": _GRPC_HTTP.get(code, 500) if type(code) is int else 500},
        "stageTimestamp": _iso(_rfc3339(entry.get("timestamp"))),
        "requestObject": proto.get("request"),
    }


def _entry_message(entry: Mapping) -> str:
    if isinstance(entry.get("protoPayload"), Mapping):
        event = gke_k8s_audit_event(entry)
        return json.dumps(
            event if event is not None else entry["protoPayload"],
            sort_keys=True,
            separators=(",", ":"),
        )
    if isinstance(entry.get("jsonPayload"), Mapping):
        return json.dumps(entry["jsonPayload"], sort_keys=True, separators=(",", ":"))
    if isinstance(entry.get("textPayload"), str):
        return entry["textPayload"]
    raise ValueError("unsupported log entry payload")


class _CloudLoggingSearch:
    """entries.list over exact log names in one project, all pages, bounded budget."""

    def __init__(
        self,
        client,
        *,
        project_id: str,
        reader_principal: str,
        log_ids: tuple[str, ...],
        resource_filter: str | None = None,
        clock=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
    ):
        if (
            not isinstance(project_id, str)
            or not PROJECT.fullmatch(project_id)
            or not isinstance(log_ids, tuple)
            or not 1 <= len(log_ids) <= 100
            or len(set(log_ids)) != len(log_ids)
            or any(not isinstance(x, str) or not _LOG_ID.fullmatch(x) for x in log_ids)
            or (
                resource_filter is not None
                and (
                    not isinstance(resource_filter, str)
                    or not 1 <= len(resource_filter) <= 2048
                    or any(ord(c) < 32 for c in resource_filter)
                )
            )
        ):
            raise ValueError("registered Cloud Logging source required")
        _gcp_principal(reader_principal)
        self.client, self.project, self.principal = client, project_id, reader_principal
        self.streams = tuple(f"projects/{project_id}/logs/{x}" for x in log_ids)
        # Server configuration (e.g. the app's k8s_container namespace), never user input.
        self.resource_filter = resource_filter
        self.source_binding = f"gcp-logging:projects/{project_id}"
        self.clock, self.monotonic = clock, monotonic
        self._validate()

    def _validate(self):
        _gcp_client(self.client, self.project, principal=self.principal)

    def _entries(self, start: datetime, end: datetime, marker: str | None):
        self._validate()
        names = " OR ".join(json.dumps(n) for n in self.streams)
        terms = [f"logName=({names})", f'timestamp>="{_iso(start)}"', f'timestamp<="{_iso(end)}"']
        if self.resource_filter is not None:
            terms.append(f"({self.resource_filter})")
        if marker is not None:
            if not TOKEN.fullmatch(marker):
                raise ValueError("registered audit marker required")
            terms.append(json.dumps(marker))
        request = {
            "resourceNames": [f"projects/{self.project}"],
            "filter": " AND ".join(terms),
            "orderBy": "timestamp asc",
            "pageSize": 1000,
        }
        rows, tokens, size = [], set(), 0
        started = self.monotonic()
        for _ in range(MAX_PAGES):
            if self.monotonic() - started >= BUDGET_SECONDS:
                raise TimeoutError("log search budget exceeded")
            try:
                page = self.client.call("logging.entries.list", dict(request))
            except GcpApiError as error:
                if (error.status, error.code) == (403, "PERMISSION_DENIED"):
                    raise PermissionError("log search denied") from None
                raise ValueError("log search failed") from None
            if self.monotonic() - started >= BUDGET_SECONDS:
                raise TimeoutError("log search budget exceeded")
            entries = page.get("entries", []) if isinstance(page, Mapping) else None
            if not isinstance(entries, list):
                raise ValueError("invalid log search response")
            for entry in entries:
                if (
                    not isinstance(entry, Mapping)
                    or not isinstance(entry.get("insertId"), str)
                    or not 1 <= len(entry["insertId"]) <= 512
                    or entry.get("logName") not in self.streams
                ):
                    raise ValueError("invalid log entry envelope")
                message = _entry_message(entry)
                size += len(message.encode())
                if size > LOG_MAX_BYTES or len(rows) >= LOG_MAX_EVENTS:
                    raise ValueError("log search exceeds budget")
                rows.append(
                    AuditEvent(
                        entry["insertId"],
                        entry["logName"],
                        _rfc3339(entry.get("timestamp")),
                        _rfc3339(entry.get("receiveTimestamp")),
                        message,
                    )
                )
            token = page.get("nextPageToken")
            if token in (None, ""):
                return tuple(rows)
            if not isinstance(token, str) or len(token) > 10000 or token in tokens:
                raise ValueError("invalid or repeated log pagination token")
            tokens.add(token)
            request["pageToken"] = token
        raise ValueError("log search page limit exceeded")


class CloudLoggingAuditSearch(_CloudLoggingSearch):
    """AUD-01 gcp: Cloud Audit Logs (GKE RBAC) and CNPG logs in Cloud Logging.

    Log IDs are e.g. ``cloudaudit.googleapis.com%2Factivity`` for AUD-01.3 and the
    container log IDs (``stdout``/``stderr``) with a CNPG resource filter for AUD-01.1/2.
    """

    def __call__(self, receipt: CanaryReceipt, through: datetime) -> AuditBatch:
        through = _utc(through)
        if not isinstance(receipt, CanaryReceipt) or not receipt.started_at <= through <= (
            receipt.deadline
        ):
            raise ValueError("search interval outside authorized canary window")
        events = self._entries(receipt.started_at, through, receipt.marker)
        return AuditBatch(
            receipt.context,
            receipt.marker,
            self.source_binding,
            self.streams,
            receipt.started_at,
            through,
            _utc(self.clock()),
            events,
            True,
        )


class CloudLoggingSecretLogSearch(_CloudLoggingSearch):
    """SEC-01.3 gcp: every app log entry in the closed canary window, no text filter.

    The host must confirm the reader sees unrestricted fields (no Cloud Logging field-level
    access restriction or exclusion on these logs); otherwise absence proves nothing.
    """

    def __call__(self, receipt: LogCanaryReceipt) -> LogWindow:
        if (
            not isinstance(receipt, LogCanaryReceipt)
            or receipt.source_binding != self.source_binding
            or receipt.streams != self.streams
            or receipt.settled_at > _utc(self.clock())
        ):
            raise ValueError("registered settled log window required")
        events = self._entries(receipt.started_at, receipt.ended_at, None)
        return LogWindow(
            receipt.context,
            self.source_binding,
            self.streams,
            receipt.started_at,
            receipt.ended_at,
            _utc(self.clock()),
            events,
            True,
            True,
        )


__all__ = [
    "CloudLoggingAuditSearch",
    "CloudLoggingSecretLogSearch",
    "GcpApiError",
    "GcpClient",
    "GcpSecretAccessProbe",
    "GcpSecretEncryptionProbe",
    "GcpSecretEncryptionTarget",
    "GcpSecretTarget",
    "GcsAnonymousReadProbe",
    "gke_k8s_audit_event",
]
