"""Bucket-policy branch: apply an approved bucket policy to the target SeaweedFS, or stop.

The rehearsal (T15) verified the T11 policy with ``put-bucket-policy`` on a separate rehearsal
bucket. On the target the exposure of the application bucket is decided by the SeaweedFS
``s3.json`` identity file that T9 renders (``infra/envs/target-workloads/object_exposure.py``).
This module connects the two without widening anything:

1. ``translate_policy`` maps the approved policy onto a T9 exposure state. Only a small,
   deterministic subset is accepted and it can only ever yield ``closed``; anything else is
   refused before the cluster is touched. T31 never renders ``open-demo``.
2. ``render_closed`` reads the identities of the *current* ``s3.json`` and asks the T9 renderer
   for the ``closed`` state with the same credentials. The result must equal the current file
   minus the ``anonymous`` identity, so the only effect of the change is removing anonymous access.
3. ``apply_bucket_policy`` replaces that one Secret, rolls that one workload, then judges by
   observation: an unsigned GET must be refused and a GET signed with the app identity must
   still succeed (control IAM-01.1). It never changes an image and never rolls back.

Secret values never leave this module: records carry sha256 digests of ``s3.json`` only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import importlib.util
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from string import Template
from types import ModuleType

from .deploy import DeployMismatch, Kube, brief

# T9 owns the renderer and the unsigned probe. Loaded from the repository by path: it is a script,
# not a package, and has only standard-library imports. parents[4] is the repository root.
T9_OBJECT_EXPOSURE = Path(__file__).resolve().parents[4] / "infra/envs/target-workloads/object_exposure.py"
AUTH_SECRET, AUTH_KEY, WORKLOAD, CONTAINER = "target-seaweedfs-auth", "s3.json", "target-seaweedfs", "seaweedfs"
APP_BUCKET = "demo-chat-attachments"
# Annotations on the workload's pod template: a new template on every application, so the pods are
# recreated and read the replaced s3.json (SeaweedFS reads it at start).
ANNOTATION_S3_JSON, ANNOTATION_APPLICATION = "bravo.dev/s3-json-sha256", "bravo.dev/applied-by"
PROBE_WINDOW_SECONDS = 90  # per probe: the store may answer 5xx or reset while the filer starts
PROBE_INTERVAL_SECONDS = 3
KNOWN_IDENTITIES = {"bootstrap", "client", "anonymous", "rehearsal-object-admin"}
ANONYMOUS_CONDITION = {"StringEquals": {"aws:PrincipalType": "Anonymous"}}
ANONYMOUS_CONDITION_LIST = {"StringEquals": {"aws:PrincipalType": ["Anonymous"]}}
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")  # same rules as T9 object_exposure
ENDPOINT_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(?::\d{1,5})?$")


class PolicyRefused(ValueError):
    """The approved policy or the target configuration cannot be applied as approved."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PolicyTarget:
    """The one Secret, workload and bucket a bucket-policy change may touch."""

    namespace: str
    endpoint: str  # S3 endpoint reachable from where T31 runs, scheme://host:port
    probe_key: str  # an existing object in ``bucket`` read by both probes
    bucket: str = APP_BUCKET
    policy_bucket: str | None = None  # bucket named in the approved policy (the rehearsal bucket); default ``bucket``
    secret: str = AUTH_SECRET
    secret_key: str = AUTH_KEY
    workload: str = WORKLOAD
    container: str = CONTAINER

    def __post_init__(self):
        # Checked before anything is recorded, so a bad target never reaches the probe after the change.
        for bucket in (self.bucket, self.policy_bucket or self.bucket):
            if not BUCKET_RE.fullmatch(bucket) or ".." in bucket:
                raise ValueError("exact lowercase bucket name required")
        if not ENDPOINT_RE.fullmatch(self.endpoint):
            raise ValueError("endpoint must be scheme://host[:port] with no path")
        key = self.probe_key
        if not key or key.startswith("/") or ".." in key.split("/") or len(key) > 512 or any(ord(c) < 32 for c in key):
            raise ValueError("probe key outside policy")

    def record(self) -> dict:
        return {"kind": "bucket_policy", **{k: ("" if v is None else v) for k, v in asdict(self).items()}}


@lru_cache(maxsize=4)
def load_t9(path: Path = T9_OBJECT_EXPOSURE) -> tuple[ModuleType, str]:
    """The T9 module and the sha256 of the file that was loaded (recorded with every outcome)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise PolicyRefused("T9_RENDERER_UNAVAILABLE") from None
    spec = importlib.util.spec_from_file_location("bravo_apply._t9_object_exposure", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not all(callable(getattr(module, name, None)) for name in ("render", "probe")):
        raise PolicyRefused("T9_RENDERER_UNAVAILABLE")
    return module, hashlib.sha256(data).hexdigest()


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _strings(value) -> list[str]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        raise PolicyRefused("POLICY_UNTRANSLATABLE")
    return values


def translate_policy(policy_text: str, *, policy_bucket: str) -> str:
    """Map the approved policy onto a T9 exposure state. Returns ``"closed"`` or raises.

    Accepted subset (anything else is ``POLICY_UNTRANSLATABLE``): ``Version`` 2012-10-17, 1–8
    statements, each with ``Effect``, ``Principal`` ``"*"``/``{"AWS": "*"}``, ``Action``
    ``s3:GetObject`` and ``Resource`` ``arn:aws:s3:::${policy_bucket}/*``, optional ``Sid`` and
    ``Condition`` ``{"StringEquals": {"aws:PrincipalType": "Anonymous"}}``.

    * ``Allow`` to everyone → ``POLICY_GRANTS_ANONYMOUS_READ`` (T31 does not open a bucket).
    * ``Deny`` to everyone without the anonymous condition → ``POLICY_DENIES_AUTHENTICATED_READ``:
      it also blocks the app identity, which the T9 identities cannot express.
    * ``Deny`` to anonymous callers only → ``closed``.
    """
    try:
        doc = json.loads(policy_text, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        raise PolicyRefused("POLICY_UNPARSEABLE") from None
    if not isinstance(doc, dict) or not set(doc) <= {"Version", "Id", "Statement"} or doc.get("Version") != "2012-10-17":
        raise PolicyRefused("POLICY_UNTRANSLATABLE")
    statements = doc.get("Statement")
    statements = [statements] if isinstance(statements, dict) else statements
    if not isinstance(statements, list) or not 1 <= len(statements) <= 8:
        raise PolicyRefused("POLICY_UNTRANSLATABLE")
    resource = Template("arn:aws:s3:::${bucket}/*").substitute(bucket=policy_bucket)
    for st in statements:
        if (not isinstance(st, dict) or not {"Effect", "Principal", "Action", "Resource"} <= set(st)
                or not set(st) <= {"Sid", "Effect", "Principal", "Action", "Resource", "Condition"}):
            raise PolicyRefused("POLICY_UNTRANSLATABLE")
        principal = st["Principal"]
        if not (principal == "*" or (isinstance(principal, dict) and set(principal) == {"AWS"}
                                     and _strings(principal["AWS"]) == ["*"])):
            raise PolicyRefused("POLICY_UNTRANSLATABLE")
        if set(_strings(st["Action"])) != {"s3:GetObject"} or set(_strings(st["Resource"])) != {resource}:
            raise PolicyRefused("POLICY_UNTRANSLATABLE")
        anonymous_only = "Condition" in st
        if anonymous_only and st["Condition"] not in (ANONYMOUS_CONDITION, ANONYMOUS_CONDITION_LIST):
            raise PolicyRefused("POLICY_UNTRANSLATABLE")
        if st["Effect"] == "Allow":
            raise PolicyRefused("POLICY_GRANTS_ANONYMOUS_READ")
        if st["Effect"] != "Deny":
            raise PolicyRefused("POLICY_UNTRANSLATABLE")
        if not anonymous_only:
            raise PolicyRefused("POLICY_DENIES_AUTHENTICATED_READ")
    return "closed"


def _credential(identity: dict) -> tuple[str, str]:
    creds = identity.get("credentials")
    if not isinstance(creds, list) or len(creds) != 1 or not isinstance(creds[0], dict):
        raise PolicyRefused("TARGET_AUTH_UNRECOGNIZED")
    access, secret = creds[0].get("accessKey"), creds[0].get("secretKey")
    if not isinstance(access, str) or not isinstance(secret, str):
        raise PolicyRefused("TARGET_AUTH_UNRECOGNIZED")
    return access, secret


def render_closed(t9: ModuleType, current: bytes, bucket: str) -> tuple[bytes, tuple[str, str]]:
    """New ``s3.json`` bytes for ``closed`` and the app (``client``) credential for the signed probe.

    The T9 renderer gets the credentials that are in the current file, never new ones. Its output
    must equal the current identities minus ``anonymous``; otherwise the target is not in a shape
    T9 renders and replacing it would change more than anonymous access."""
    try:
        doc = json.loads(current, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        raise PolicyRefused("TARGET_AUTH_UNRECOGNIZED") from None
    identities = doc.get("identities") if isinstance(doc, dict) and set(doc) == {"identities"} else None
    if not isinstance(identities, list) or not all(isinstance(i, dict) for i in identities):
        raise PolicyRefused("TARGET_AUTH_UNRECOGNIZED")
    by_name = {i.get("name"): i for i in identities}
    if len(by_name) != len(identities) or not set(by_name) <= KNOWN_IDENTITIES or not {"bootstrap", "client"} <= set(by_name):
        raise PolicyRefused("TARGET_AUTH_UNRECOGNIZED")
    environ = {}
    for name, (access_var, secret_var) in t9.ENV.items():
        environ[access_var], environ[secret_var] = _credential(by_name[name])
    rehearsal_bucket = None
    if "rehearsal-object-admin" in by_name:
        admin = by_name["rehearsal-object-admin"]
        environ[t9.REHEARSAL_ENV[0]], environ[t9.REHEARSAL_ENV[1]] = _credential(admin)
        actions = admin.get("actions")
        if not isinstance(actions, list) or not actions or not isinstance(actions[0], str) or ":" not in actions[0]:
            raise PolicyRefused("TARGET_AUTH_UNRECOGNIZED")
        rehearsal_bucket = actions[0].split(":", 1)[1]
    try:
        rendered = t9.render("closed", bucket, environ, rehearsal_bucket=rehearsal_bucket)
    except ValueError:
        raise PolicyRefused("TARGET_AUTH_UNRECOGNIZED") from None
    if rendered["identities"] != [i for i in identities if i.get("name") != "anonymous"]:
        raise PolicyRefused("TARGET_AUTH_UNRECOGNIZED")
    # Same serialisation as ``object_exposure.py render``.
    return (json.dumps(rendered, indent=2) + "\n").encode("utf-8"), _credential(by_name["client"])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect not followed", headers, fp)


_DIRECT = urllib.request.build_opener(_NoRedirect())


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def signed_get(endpoint: str, bucket: str, key: str, access: str, secret: str, *, region: str = "us-east-1",
               opener=_DIRECT.open, timeout: float = 10.0, now: datetime | None = None) -> dict:
    """GET of one object signed with AWS SigV4 (header form). 200 → ALLOWED, 401/403 → DENIED,
    anything else UNKNOWN. Never retains the body; never logs the credential."""
    parsed = urllib.parse.urlsplit(endpoint)
    path = f"/{bucket}/{urllib.parse.quote(key, safe='/')}"
    amz_date = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    payload = hashlib.sha256(b"").hexdigest()
    headers = {"host": parsed.netloc, "x-amz-content-sha256": payload, "x-amz-date": amz_date}
    signed = ";".join(sorted(headers))
    canonical = "\n".join(["GET", path, "", *(f"{k}:{headers[k]}" for k in sorted(headers)), "", signed, payload])
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    signing_key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode("utf-8"), amz_date[:8]), region), "s3"), "aws4_request")
    signature = hmac.new(signing_key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    request = urllib.request.Request(f"{parsed.scheme}://{parsed.netloc}{path}", method="GET", headers={
        "Authorization": f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={signature}",
        "x-amz-content-sha256": payload, "x-amz-date": amz_date})
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            response.read(1)
    except urllib.error.HTTPError as error:
        status = error.code
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as error:
        # A restarting store can cut a response short (BadStatusLine, IncompleteRead): retry it.
        return {"authenticated_get": f"UNKNOWN:{type(error).__name__}"}
    outcome = "ALLOWED" if status == 200 else "DENIED" if status in (401, 403) else f"UNKNOWN:{status}"
    return {"authenticated_get": outcome}


def _until_known(call: Callable[[], dict], field: str, *, window: float, interval: float,
                 sleep: Callable[[float], None], monotonic: Callable[[], float]) -> dict:
    """Repeat a probe while its answer is UNKNOWN (store restarting), up to ``window`` seconds."""
    deadline = monotonic() + window
    while True:
        result = call()
        if not result[field].startswith("UNKNOWN") or monotonic() >= deadline:
            return result
        sleep(interval)


def _secret_bytes(secret: dict, key: str) -> bytes:
    data = secret.get("data") or {}
    if key not in data:
        raise LookupError(f"key {key!r} not in secret")
    return base64.b64decode(data[key], validate=True)


def apply_bucket_policy(kube: Kube, target: PolicyTarget, *, t9: ModuleType, renderer_sha256: str, application_id: str,
                        timeout_seconds: int = 300, anonymous_probe: Callable[[], dict] | None = None,
                        signed_probe: Callable[[str, str], dict] | None = None,
                        probe_window: float = PROBE_WINDOW_SECONDS, probe_interval: float = PROBE_INTERVAL_SECONDS,
                        sleep: Callable[[float], None] = time.sleep,
                        monotonic: Callable[[], float] = time.monotonic) -> dict:
    """Read, render ``closed``, replace the Secret, roll the workload, probe. The approved policy
    has already been translated (pure) by the caller.

    Raises ``DeployMismatch`` for every failure; ``mutation_attempted`` is True from the Secret
    replace onward. The workload image is read for the record and never changed."""
    policy = {"state": "closed", "renderer_sha256": renderer_sha256, "previous_s3_json_sha256": None,
              "applied_s3_json_sha256": None, "anonymous_get": None, "authenticated_get": None}

    def refuse(code, exc=None, *, type_only=False):
        # type_only: the exception came from parsing credentials, so its text is not logged.
        detail = (type(exc).__name__ if type_only else brief(exc)) if exc else ""
        return DeployMismatch(code, mutation_attempted=False, detail=detail, policy=dict(policy))

    try:
        secret = kube.get_secret(target.namespace, target.secret)
        current = _secret_bytes(secret, target.secret_key)
        workload_image = kube.current_image(target.namespace, target.workload, target.container)
    except Exception as exc:  # noqa: BLE001 - any read failure means "nothing was changed"
        raise refuse("CLUSTER_READ_FAILED", exc) from exc
    policy["previous_s3_json_sha256"] = hashlib.sha256(current).hexdigest()
    # Nothing has been changed yet: an unexpected shape here is a refusal, not an unknown outcome.
    try:
        new, (access, secret_key) = render_closed(t9, current, target.bucket)
    except PolicyRefused as exc:
        raise refuse(exc.code) from exc
    except Exception as exc:  # noqa: BLE001
        raise refuse("TARGET_AUTH_UNRECOGNIZED", exc, type_only=True) from exc
    applied = hashlib.sha256(new).hexdigest()
    try:
        replacement = json.loads(json.dumps(secret))
        replacement["metadata"].pop("managedFields", None)
        replacement["data"][target.secret_key] = base64.b64encode(new).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        raise refuse("TARGET_SECRET_UNRECOGNIZED", exc, type_only=True) from exc

    def hold(code, exc=None, **extra):
        return DeployMismatch(code, mutation_attempted=True, previous_image=workload_image,
                              detail=brief(exc) if exc else extra.pop("detail", ""), policy=dict(policy))

    policy["applied_s3_json_sha256"] = applied
    try:
        kube.replace_secret(target.namespace, replacement)
    except Exception as exc:  # noqa: BLE001 - the API server may have accepted it anyway
        raise hold("SECRET_REPLACE_FAILED", exc) from exc
    try:
        kube.restart_workload(target.namespace, target.workload,
                              {ANNOTATION_S3_JSON: applied, ANNOTATION_APPLICATION: application_id})
    except Exception as exc:  # noqa: BLE001
        raise hold("RESTART_FAILED", exc) from exc
    try:
        complete = kube.rollout_status(target.namespace, target.workload, timeout_seconds)
    except Exception as exc:  # noqa: BLE001
        raise hold("ROLLOUT_STATUS_FAILED", exc) from exc
    if not complete:
        raise hold("ROLLOUT_NOT_COMPLETE")
    def t9_anonymous_probe():
        try:
            return t9.probe(target.endpoint, target.bucket, target.probe_key)
        except http.client.HTTPException as exc:  # cut-short response while restarting; T9 lets it through
            return {"anonymous_get": f"UNKNOWN:{type(exc).__name__}"}

    anonymous_probe = anonymous_probe or t9_anonymous_probe
    signed_probe = signed_probe or (lambda a, s: signed_get(target.endpoint, target.bucket, target.probe_key, a, s))
    timing = {"window": probe_window, "interval": probe_interval, "sleep": sleep, "monotonic": monotonic}
    try:
        anonymous = _until_known(anonymous_probe, "anonymous_get", **timing)
    except Exception as exc:  # noqa: BLE001 - e.g. an endpoint T9 rejects; the Secret is already replaced
        raise hold("PROBE_FAILED", exc) from exc
    policy["anonymous_get"] = anonymous["anonymous_get"]
    if anonymous["anonymous_get"] == "ALLOWED":
        raise hold("ANONYMOUS_READ_STILL_ALLOWED")
    if anonymous["anonymous_get"] != "DENIED":
        raise hold("PROBE_UNKNOWN", detail=anonymous["anonymous_get"])
    try:
        authenticated = _until_known(lambda: signed_probe(access, secret_key), "authenticated_get", **timing)
    except Exception as exc:  # noqa: BLE001
        raise hold("PROBE_FAILED", exc) from exc
    policy["authenticated_get"] = authenticated["authenticated_get"]
    if authenticated["authenticated_get"] != "ALLOWED":
        raise hold("AUTHENTICATED_READ_FAILED", detail=authenticated["authenticated_get"])
    return {"previous_image": workload_image, "policy": policy}
