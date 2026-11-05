"""Bucket-policy verification Jobs in the tested rehearsal namespace.

Platform-held scripts run in a separate Job with the namespace's tokenless
default account, never the agent's tool account. The host supplies the
dedicated test bucket, its credential and network reachability; this module
does not widen target access rules or touch any other bucket. The returned
evidence is produced by the trusted runner, not the model, and still needs the
attempt record before T10 may read it.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from .bucket_proposal_source import BoundBucketProposalSource
from .bucket_verifier import BucketVerificationRequest
from .credential_candidate import file_diff
from .recovery_lookup import recovery_identity

# Each Job applies one policy and then reads the canary signed, unsigned, signed.
# The script always exits 0 after printing; the runner judges the observation.
SCRIPT = '''set -u
applied=no; before=unknown; anon=unknown; after=unknown
if aws --endpoint-url "$ENDPOINT" s3api put-bucket-policy --bucket "$BUCKET" --policy file:///policies/"$POLICY" >/dev/null 2>&1; then applied=yes; fi
if aws --endpoint-url "$ENDPOINT" s3api get-object --bucket "$BUCKET" --key "$KEY" /tmp/before >/dev/null 2>&1; then
  if [ "$(sha256sum /tmp/before | cut -d ' ' -f 1)" = "$CANARY_SHA256" ]; then before=matches; else before=differs; fi
else before=failed; fi
if aws --endpoint-url "$ENDPOINT" --no-sign-request s3api get-object --bucket "$BUCKET" --key "$KEY" /tmp/anon >/dev/null 2>/tmp/anon.err; then
  anon=allowed
elif grep -Eq '403|AccessDenied' /tmp/anon.err; then anon=denied; fi
if aws --endpoint-url "$ENDPOINT" s3api get-object --bucket "$BUCKET" --key "$KEY" /tmp/after >/dev/null 2>&1; then
  if [ "$(sha256sum /tmp/after | cut -d ' ' -f 1)" = "$CANARY_SHA256" ]; then after=matches; else after=differs; fi
else after=failed; fi
printf '{"applied":"%s","authenticated_before":"%s","anonymous":"%s","authenticated_after":"%s"}\\n' "$applied" "$before" "$anon" "$after"
'''
OBSERVATION_KEYS = {"applied", "authenticated_before", "anonymous", "authenticated_after"}


@dataclass(frozen=True)
class BucketFixture:
    """Dedicated test bucket prepared by the host (T9), not an application bucket."""

    endpoint: str
    bucket: str
    canary_key: str
    canary_sha256: str
    image: str

    def __post_init__(self):
        if (re.fullmatch(r"https?://[A-Za-z0-9.-]+:[0-9]{1,5}", self.endpoint) is None
                or re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket) is None
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.canary_key) is None
                or re.fullmatch(r"[a-f0-9]{64}", self.canary_sha256) is None
                or re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", self.image) is None):
            raise ValueError("invalid bucket fixture")


@dataclass(frozen=True)
class BucketEvidence:
    verdict: Literal["PASS", "FAIL"]
    reason: str
    namespace: str
    namespace_uid: str
    proposal_digest: str
    diff_hash: str
    image_digest: str
    observations: dict
    executions: dict
    diff: bytes = field(repr=False, compare=False)

    def public(self) -> dict:
        """Evidence fields safe to persist; no credential or policy body."""
        return {"verdict": self.verdict, "reason": self.reason, "namespace": self.namespace,
                "namespace_uid": self.namespace_uid, "proposal_digest": self.proposal_digest,
                "diff_hash": self.diff_hash, "image_digest": self.image_digest,
                "observations": self.observations, "executions": self.executions}


class VerificationUnknown(RuntimeError):
    """The environment, not the proposal, prevented a definite verdict."""


def judge(control: dict, candidate: dict) -> tuple[Literal["PASS", "FAIL"], str]:
    """Return a definite verdict or raise when the environment is inconclusive."""
    if control != {"applied": "yes", "authenticated_before": "matches", "anonymous": "allowed",
                   "authenticated_after": "matches"}:
        # Without an anonymous-allowed control, a later denial proves nothing.
        raise VerificationUnknown("control_unconfirmed")
    if candidate["applied"] != "yes":
        return "FAIL", "POLICY_REJECTED"
    if "unknown" in candidate.values():
        raise VerificationUnknown("observation_unknown")
    if candidate["anonymous"] == "allowed":
        return "FAIL", "ANONYMOUS_READ_ALLOWED"
    if candidate["authenticated_before"] != "matches" or candidate["authenticated_after"] != "matches":
        return "FAIL", "AUTHENTICATED_READ_BROKEN"
    return "PASS", "NONE"


@dataclass(frozen=True)
class SeaweedBucketJob:
    """Product verification for one attempt; `kube` is T38's `verification.Kube`."""

    kube: object = field(repr=False)
    proposals: BoundBucketProposalSource = field(repr=False)
    snapshot: object = field(repr=False)
    fixture: BucketFixture
    credential: Callable[[], tuple[str, str]] = field(repr=False)
    source_policy_path: str = "src/source-policy.json"
    poll_seconds: float = 2.0

    def verify(self, request: BucketVerificationRequest) -> BucketEvidence:
        namespace = request.namespace
        self._require_namespace(namespace)
        if request.target_bucket != self.fixture.bucket:
            raise ValueError("request outside the test bucket")
        resolved = self.proposals.resolve(request)
        path = self.proposals.target_policy_path
        before = self.snapshot.read(path)
        if (not isinstance(before, str)
                or hashlib.sha256(before.encode("utf-8")).hexdigest() != resolved.before_sha256):
            raise ValueError("proposal base does not match the snapshot")
        source = self.snapshot.read(self.source_policy_path)
        if hashlib.sha256(source.encode("utf-8")).hexdigest() != request.source_policy_sha256:
            raise ValueError("source policy changed")
        diff = file_diff(path, before, resolved.replacement)
        if not diff:
            raise ValueError("candidate contains no changes")
        access, secret = self.credential()
        labels, annotations = recovery_identity(request.run_id, request.attempt)
        meta = {"namespace": namespace.name, "labels": labels, "annotations": annotations}
        self.kube.create({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                          "metadata": meta | {"name": "bucket-verifier-auth"},
                          "stringData": {"access": access, "secret": secret}})
        self.kube.create({"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                          "metadata": meta | {"name": "bucket-policies"},
                          "data": {"source.json": source, "candidate.json": resolved.replacement}})
        observations, executions = {}, {}
        for name, policy in (("bucket-control", "source.json"), ("bucket-candidate", "candidate.json")):
            observations[name], executions[name] = self._run(namespace, name, policy, meta)
        self._require_namespace(namespace)
        verdict, reason = judge(observations["bucket-control"], observations["bucket-candidate"])
        return BucketEvidence(verdict, reason, namespace.name, namespace.uid, request.proposal_digest,
                              "sha256:" + hashlib.sha256(diff).hexdigest(),
                              "sha256:" + self.fixture.image.split("@sha256:")[1],
                              observations, executions, diff)

    def _require_namespace(self, namespace) -> None:
        meta = self.kube.get("namespace", namespace.name)["metadata"]
        if meta.get("uid") != namespace.uid or meta.get("deletionTimestamp"):
            raise VerificationUnknown("namespace_identity_changed")

    def _job(self, namespace, name: str, policy: str, meta: dict) -> dict:
        fixture = self.fixture
        env = [{"name": key, "value": value} for key, value in (
            ("ENDPOINT", fixture.endpoint), ("BUCKET", fixture.bucket), ("KEY", fixture.canary_key),
            ("CANARY_SHA256", fixture.canary_sha256), ("POLICY", policy), ("HOME", "/tmp"),
            ("AWS_DEFAULT_REGION", "us-east-1"), ("AWS_EC2_METADATA_DISABLED", "true"),
            ("AWS_MAX_ATTEMPTS", "1"))]
        env += [{"name": key, "valueFrom": {"secretKeyRef": {"name": "bucket-verifier-auth", "key": part}}}
                for key, part in (("AWS_ACCESS_KEY_ID", "access"), ("AWS_SECRET_ACCESS_KEY", "secret"))]
        container = {"name": "run", "image": fixture.image, "imagePullPolicy": "IfNotPresent",
                     "command": ["/bin/sh", "-c", SCRIPT], "env": env,
                     "resources": {"requests": {"cpu": "20m", "memory": "96Mi"},
                                   "limits": {"cpu": "500m", "memory": "256Mi"}},
                     "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                         "capabilities": {"drop": ["ALL"]}},
                     "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"},
                                      {"name": "policies", "mountPath": "/policies", "readOnly": True}]}
        return {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta | {"name": name},
                "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 180, "template": {
                    "metadata": {"labels": {"role": "bucket-verifier"}},
                    "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                             "serviceAccountName": "default",
                             "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
                                                 "seccompProfile": {"type": "RuntimeDefault"}},
                             "volumes": [{"name": "tmp", "emptyDir": {}},
                                         {"name": "policies", "configMap": {"name": "bucket-policies"}}],
                             "containers": [container]}}}}

    def _run(self, namespace, name: str, policy: str, meta: dict) -> tuple[dict, dict]:
        from bravo_agent.verification import observe_job_pod

        self._require_namespace(namespace)
        created = self.kube.create(self._job(namespace, name, policy, meta))
        uid = created["metadata"]["uid"]
        for _ in range(100):
            self._require_namespace(namespace)
            current = self.kube.get("job", name, namespace.name)
            if current["metadata"]["uid"] != uid:
                raise VerificationUnknown("job_identity_changed")
            status = current.get("status", {})
            if status.get("failed"):
                raise VerificationUnknown("job_failed")
            if status.get("succeeded"):
                break
            time.sleep(self.poll_seconds)
        else:
            raise VerificationUnknown("job_timeout")
        execution = observe_job_pod(self.kube, created, self.fixture.image, complete=True)
        log = self.kube.call("logs", "pod/" + execution["pod_name"], "-n", namespace.name, "-c", "run",
                             "--limit-bytes=2048")
        if observe_job_pod(self.kube, created, self.fixture.image, complete=True) != execution:
            raise VerificationUnknown("job_execution_changed")
        observation = json.loads(log)
        if (not isinstance(observation, dict) or set(observation) != OBSERVATION_KEYS
                or any(not isinstance(value, str) for value in observation.values())):
            raise VerificationUnknown("invalid_observation")
        record = {key: execution[key] for key in ("job_uid", "pod_uid", "container_id", "image_id", "exit_code")}
        record["log_sha256"] = hashlib.sha256(log.encode("utf-8")).hexdigest()
        return observation, record
