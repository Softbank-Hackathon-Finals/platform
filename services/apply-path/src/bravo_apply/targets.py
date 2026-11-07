"""Bucket-policy appliers by target kind (ADR-0034 `target.neutral_apply`, apply side).

* onprem: the existing T9 path (``policy.translate_policy`` → ``policy.apply_bucket_policy``),
  unchanged byte for byte.
* aws / gcp: the approved policy is the rehearsal-verified render in the target's own dialect
  (S3 bucket policy / GCS bucket IAM). It is exported to the neutral model and rendered again
  with A5's renderer; the change is applied only if that render is *exactly* the approved bytes
  (same digest as the binding's ``policy_digest``). The render for the application bucket is
  then written through a ``BucketPolicyStore`` and judged by observation like onprem: an
  unsigned GET must be refused and the app identity must still read (IAM-01.1).

The neutral package is A5's ``rehearsal_runner/neutral`` loaded by path as its own package (it
is standard-library only), so apply-path does not install the rehearsal runtime. Its sha256 is
recorded with every outcome like T9's renderer.

Cloud SDKs are not dependencies: ``Boto3S3PolicyStore`` and ``GcsIamPolicyStore`` take a client
the host built (with its own short-lived identity). Tests use fakes.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Protocol

from .binding import APP_IMAGE, BUCKET_POLICY
from .deploy import DeployMismatch, brief
from .policy import (
    BUCKET_RE,
    PROBE_INTERVAL_SECONDS,
    PROBE_WINDOW_SECONDS,
    PolicyRefused,
    PolicyTarget,
    _until_known,
    apply_bucket_policy,
    load_t9,
    translate_policy,
)

NEUTRAL_DIR = Path(__file__).resolve().parents[4] / "services/rehearsal-runner/src/rehearsal_runner/neutral"
CLOUD_KINDS = ("aws", "gcp")
ENDPOINT_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::\d{1,5})?$")

# Gate recheck after an applied change. The test ids are kind-neutral (docs/controls); the gate
# picks the probe by (target kind, test id) (`both.control_probes`, A7), so the request names the
# kind whenever it is not the original onprem target.
RECHECK_TESTS = {
    APP_IMAGE: {"onprem": ["SEC-01.1", "IAM-01.1", "ENC-01.1"], "aws": ["SEC-01.1", "IAM-01.1", "ENC-01.1"],
                "gcp": ["SEC-01.1", "IAM-01.1", "ENC-01.1"]},
    # A bucket-policy change only changes who may read attachment objects.
    BUCKET_POLICY: {"onprem": ["IAM-01.1"], "aws": ["IAM-01.1"], "gcp": ["IAM-01.1"]},
}


def recheck_tests(change_kind: str, target_kind: str) -> list[str]:
    try:
        return list(RECHECK_TESTS[change_kind][target_kind])
    except KeyError:
        raise PolicyRefused("TARGET_KIND_UNSUPPORTED") from None


@lru_cache(maxsize=2)
def load_neutral(path: Path = NEUTRAL_DIR) -> tuple[ModuleType, str]:
    """A5's neutral package and the sha256 over its files (name + bytes, sorted)."""
    path = Path(path)
    try:
        files = sorted(path.glob("*.py"))
        digest = hashlib.sha256()
        for file in files:
            digest.update(file.name.encode() + b"\0" + file.read_bytes() + b"\0")
    except OSError:
        raise PolicyRefused("NEUTRAL_RENDERER_UNAVAILABLE") from None
    if not (path / "__init__.py").is_file():
        raise PolicyRefused("NEUTRAL_RENDERER_UNAVAILABLE")
    name = "bravo_apply._neutral"
    spec = importlib.util.spec_from_file_location(name, path / "__init__.py", submodule_search_locations=[str(path)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # relative imports inside the package resolve against this name
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    if not all(callable(getattr(module, n, None)) for n in ("export_object_policy", "render_object_policy")):
        raise PolicyRefused("NEUTRAL_RENDERER_UNAVAILABLE")
    return module, digest.hexdigest()


@dataclass(frozen=True)
class CloudPolicyTarget:
    """The one bucket a bucket-policy change may touch on an aws or gcp target.

    ``identities`` maps a workload ``(namespace, service_account)`` to its principal on this target
    (IRSA role ARN / IAM member). The approved policy must name only these."""

    target_kind: str
    endpoint: str  # public object endpoint for the unsigned probe, https://host[:port], path-style
    probe_key: str
    bucket: str
    policy_bucket: str | None = None
    identities: tuple[tuple[str, str, str], ...] = ()  # (namespace, service_account, principal)

    def __post_init__(self):
        if self.target_kind not in CLOUD_KINDS:
            raise ValueError("cloud target kind required")
        for bucket in (self.bucket, self.policy_bucket or self.bucket):
            if not BUCKET_RE.fullmatch(bucket) or ".." in bucket:
                raise ValueError("exact lowercase bucket name required")
        if not ENDPOINT_RE.fullmatch(self.endpoint):
            raise ValueError("endpoint must be https://host[:port] with no path")
        key = self.probe_key
        if not key or key.startswith("/") or ".." in key.split("/") or len(key) > 512 or any(ord(c) < 32 for c in key):
            raise ValueError("probe key outside policy")

    def record(self) -> dict:
        return {"kind": "bucket_policy", "target_kind": self.target_kind, "endpoint": self.endpoint,
                "probe_key": self.probe_key, "bucket": self.bucket, "policy_bucket": self.policy_bucket or ""}

    def principal_for(self, neutral: ModuleType) -> Callable:
        table = {(ns, sa): principal for ns, sa, principal in self.identities}
        return lambda w: table.get((w.namespace, w.service_account))

    def principals(self, neutral: ModuleType) -> dict:
        return {principal: neutral.Workload(ns, sa) for ns, sa, principal in self.identities}


def target_kind(target) -> str:
    return target.target_kind if isinstance(target, CloudPolicyTarget) else "onprem"


# --- stores --------------------------------------------------------------------------------------

class BucketPolicyStore(Protocol):
    def read(self, bucket: str) -> str | None: ...
    def write(self, bucket: str, text: str) -> None: ...
    def authenticated_get(self, bucket: str, key: str) -> str: ...  # ALLOWED | DENIED | UNKNOWN:<why>


@dataclass(frozen=True)
class Boto3S3PolicyStore:
    """``client`` is a boto3 S3 client the host created with the apply identity."""

    client: object = field(repr=False)

    def read(self, bucket: str) -> str | None:
        try:
            return self.client.get_bucket_policy(Bucket=bucket)["Policy"]
        except Exception as exc:  # noqa: BLE001 - botocore is not a dependency here
            if _error_code(exc) == "NoSuchBucketPolicy":
                return None
            raise

    def write(self, bucket: str, text: str) -> None:
        self.client.put_bucket_policy(Bucket=bucket, Policy=text)

    def authenticated_get(self, bucket: str, key: str) -> str:
        try:
            body = self.client.get_object(Bucket=bucket, Key=key, Range="bytes=0-0")["Body"]
            body.read(1)
            body.close()
            return "ALLOWED"
        except Exception as exc:  # noqa: BLE001
            code = _error_code(exc)
            return "DENIED" if code in ("AccessDenied", "403") else f"UNKNOWN:{code or type(exc).__name__}"


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    return str((response or {}).get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""


def merge_gcs_bindings(current: list[dict], rendered: list[dict], managed: set[str]) -> list[dict]:
    """Rendered bindings replace every binding of the members the policy manages (the public and
    the mapped workloads). Other members (project owners, unrelated identities) keep theirs, so
    applying never removes the bucket owner's access."""
    out = []
    for binding in current:
        members = [m for m in binding.get("members", []) if m not in managed]
        if members:
            out.append({**binding, "members": members})
    out.extend(rendered)
    return out


@dataclass(frozen=True)
class GcsIamPolicyStore:
    """``client`` is a ``google.cloud.storage.Client`` the host created with the apply identity."""

    client: object = field(repr=False)

    def read(self, bucket: str) -> str | None:
        policy = self.client.bucket(bucket).get_iam_policy(requested_policy_version=3)
        return json.dumps({"version": policy.version, "bindings": list(policy.bindings)}, sort_keys=True)

    def write(self, bucket: str, text: str) -> None:
        rendered = json.loads(text)
        target = self.client.bucket(bucket)
        policy = target.get_iam_policy(requested_policy_version=3)  # keeps the etag: a concurrent change fails
        managed = {m for b in rendered["bindings"] for m in b["members"]} | {"allUsers", "allAuthenticatedUsers"}
        policy.bindings = merge_gcs_bindings(list(policy.bindings), rendered["bindings"], managed)
        policy.version = max(int(policy.version or 1), rendered["version"])
        target.set_iam_policy(policy)

    def authenticated_get(self, bucket: str, key: str) -> str:
        try:
            self.client.bucket(bucket).blob(key).download_as_bytes(start=0, end=0)
            return "ALLOWED"
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "code", None)
            return "DENIED" if status in (401, 403) else f"UNKNOWN:{status or type(exc).__name__}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect not followed", headers, fp)


_DIRECT = urllib.request.build_opener(_NoRedirect())


def anonymous_get(endpoint: str, bucket: str, key: str, *, opener=_DIRECT.open, timeout: float = 10.0) -> dict:
    """Unsigned path-style GET of one object. 200 → ALLOWED, 401/403 → DENIED, else UNKNOWN."""
    request = urllib.request.Request(f"{endpoint}/{bucket}/{urllib.parse.quote(key, safe='/')}", method="GET")
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            response.read(1)
    except urllib.error.HTTPError as error:
        status = error.code
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as error:
        return {"anonymous_get": f"UNKNOWN:{type(error).__name__}"}
    return {"anonymous_get": "ALLOWED" if status == 200 else "DENIED" if status in (401, 403) else f"UNKNOWN:{status}"}


# --- appliers ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Prepared:
    """Pure result of checking the approved policy; nothing has been touched."""

    kind: str
    detail: str
    context: dict = field(repr=False)


class OnpremApplier:
    kind = "onprem"

    def prepare(self, approved: str, target: PolicyTarget) -> Prepared:
        state = translate_policy(approved, policy_bucket=target.policy_bucket or target.bucket)
        t9, t9_sha = load_t9()
        return Prepared("onprem", f"T9 state {state}", {"t9": t9, "renderer_sha256": t9_sha})

    def apply(self, kube, target: PolicyTarget, prepared: Prepared, *, application_id: str, timeout_seconds: int,
              **hooks) -> dict:
        return apply_bucket_policy(kube, target, t9=prepared.context["t9"],
                                   renderer_sha256=prepared.context["renderer_sha256"],
                                   application_id=application_id, timeout_seconds=timeout_seconds, **hooks)


@dataclass(frozen=True)
class CloudApplier:
    """``store`` is the ``BucketPolicyStore`` for the target kind."""

    kind: str
    store: BucketPolicyStore = field(repr=False)

    def prepare(self, approved: str, target: CloudPolicyTarget) -> Prepared:
        if target.target_kind != self.kind:
            raise PolicyRefused("TARGET_KIND_MISMATCH")
        neutral, neutral_sha = load_neutral()
        policy_bucket = target.policy_bucket or target.bucket
        try:
            model = neutral.export_object_policy(self.kind, approved, bucket=policy_bucket,
                                                 principals=target.principals(neutral))
            again = neutral.render_object_policy(self.kind, model, principal_for=target.principal_for(neutral),
                                                 bucket=policy_bucket)
        except neutral.NeutralError as exc:
            raise PolicyRefused("POLICY_UNTRANSLATABLE") from exc
        if model.public_read or model.public_list:
            raise PolicyRefused("POLICY_GRANTS_ANONYMOUS_READ")  # T31 never opens a bucket
        if model.unresolved:
            raise PolicyRefused("POLICY_PRINCIPAL_UNRESOLVED")
        if again.text() != approved:
            # Only a canonical render of the neutral model is applied: nothing the renderer would
            # drop or reorder can ride along with the approval.
            raise PolicyRefused("POLICY_NOT_CANONICAL_RENDER")
        rendered = neutral.render_object_policy(self.kind, model, principal_for=target.principal_for(neutral),
                                                bucket=target.bucket)
        if rendered.skipped:
            raise PolicyRefused("POLICY_PRINCIPAL_UNRESOLVED")
        return Prepared(self.kind, f"neutral render {self.kind}", {
            "neutral": neutral, "model": model, "approved_digest": again.digest(), "approved": approved,
            "text": rendered.text(), "renderer_sha256": neutral_sha, "principal_for": target.principal_for(neutral),
            "policy_bucket": policy_bucket})

    def apply(self, kube, target: CloudPolicyTarget, prepared: Prepared, *, application_id: str,
              timeout_seconds: int, anonymous_probe: Callable[[], dict] | None = None,
              signed_probe: Callable[[], dict] | None = None, probe_window: float = PROBE_WINDOW_SECONDS,
              probe_interval: float = PROBE_INTERVAL_SECONDS, sleep: Callable[[float], None] = time.sleep,
              monotonic: Callable[[], float] = time.monotonic) -> dict:
        """Digest check → write → read back → probe. ``kube`` is unused (no cluster object changes)."""
        ctx, neutral = prepared.context, prepared.context["neutral"]
        text = ctx["text"]
        policy = {"state": "applied", "target_kind": self.kind, "renderer_sha256": ctx["renderer_sha256"],
                  "previous_policy_sha256": None, "applied_policy_sha256": hashlib.sha256(text.encode()).hexdigest(),
                  "anonymous_get": None, "authenticated_get": None}

        def refuse(code, exc=None):
            return DeployMismatch(code, mutation_attempted=False, detail=brief(exc) if exc else "", policy=dict(policy))

        def hold(code, exc=None, detail=""):
            return DeployMismatch(code, mutation_attempted=True, detail=brief(exc) if exc else detail, policy=dict(policy))

        # The approved bytes must still be the render of the same model right before the write.
        again = neutral.render_object_policy(self.kind, ctx["model"], principal_for=ctx["principal_for"],
                                             bucket=ctx["policy_bucket"])
        if again.digest() != ctx["approved_digest"] or again.text() != ctx["approved"]:
            raise refuse("RENDER_DIGEST_MISMATCH")
        try:
            previous = self.store.read(target.bucket)
        except Exception as exc:  # noqa: BLE001 - nothing was changed
            raise refuse("CLUSTER_READ_FAILED", exc) from exc
        policy["previous_policy_sha256"] = None if previous is None else hashlib.sha256(previous.encode()).hexdigest()
        try:
            self.store.write(target.bucket, text)
        except Exception as exc:  # noqa: BLE001 - the API may have accepted it anyway
            raise hold("POLICY_WRITE_FAILED", exc) from exc
        try:
            readback = self.store.read(target.bucket)
            observed = neutral.export_object_policy(self.kind, readback or "{}", bucket=target.bucket,
                                                    principals=target.principals(neutral))
        except Exception as exc:  # noqa: BLE001
            raise hold("POLICY_READBACK_FAILED", exc) from exc
        wanted = {g for g in ctx["model"].grants}
        if not wanted <= set(observed.grants) or observed.public_read or observed.public_list:
            raise hold("POLICY_READBACK_MISMATCH")
        anonymous_probe = anonymous_probe or (lambda: anonymous_get(target.endpoint, target.bucket, target.probe_key))
        signed_probe = signed_probe or (lambda: {"authenticated_get": self.store.authenticated_get(target.bucket,
                                                                                                    target.probe_key)})
        timing = {"window": probe_window, "interval": probe_interval, "sleep": sleep, "monotonic": monotonic}
        try:
            anonymous = _until_known(anonymous_probe, "anonymous_get", **timing)
        except Exception as exc:  # noqa: BLE001
            raise hold("PROBE_FAILED", exc) from exc
        policy["anonymous_get"] = anonymous["anonymous_get"]
        if anonymous["anonymous_get"] == "ALLOWED":
            raise hold("ANONYMOUS_READ_STILL_ALLOWED")
        if anonymous["anonymous_get"] != "DENIED":
            raise hold("PROBE_UNKNOWN", detail=anonymous["anonymous_get"])
        try:
            authenticated = _until_known(signed_probe, "authenticated_get", **timing)
        except Exception as exc:  # noqa: BLE001
            raise hold("PROBE_FAILED", exc) from exc
        policy["authenticated_get"] = authenticated["authenticated_get"]
        if authenticated["authenticated_get"] != "ALLOWED":
            raise hold("AUTHENTICATED_READ_FAILED", detail=authenticated["authenticated_get"])
        return {"previous_image": None, "policy": policy}


def policy_applier(target, stores: dict[str, BucketPolicyStore] | None = None):
    """The applier for ``target``'s kind. A cloud kind without a store is refused (nothing touched)."""
    kind = target_kind(target)
    if kind == "onprem":
        return OnpremApplier()
    store = (stores or {}).get(kind)
    if store is None:
        raise PolicyRefused("TARGET_APPLIER_UNAVAILABLE")
    return CloudApplier(kind, store)
