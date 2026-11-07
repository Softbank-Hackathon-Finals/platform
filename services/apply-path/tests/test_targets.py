"""Bucket-policy appliers by target kind (ADR-0034 A6). Cloud stores are fakes; nothing leaves the process."""

import hashlib
import json

import pytest

from bravo_apply.binding import diff_hash, policy_digest, render_diff
from bravo_apply.deploy import FakeKube
from bravo_apply.path import GATE_RECHECK_TESTS, POLICY_GATE_RECHECK_TESTS, apply, validate_outcome
from bravo_apply.policy import PolicyRefused, PolicyTarget
from bravo_apply.targets import (
    CloudApplier,
    CloudPolicyTarget,
    OnpremApplier,
    anonymous_get,
    load_neutral,
    merge_gcs_bindings,
    policy_applier,
    recheck_tests,
)
from tests.conftest import IMAGE, NOW

POLICY_FILE = "src/target-policy.json"
APP_BUCKET, REHEARSAL = "demo-chat-attachments", "bravo-rehearsal-objects"
AWS_ROLE = "arn:aws:iam::111122223333:role/demo-chat"  # AWS documentation example account
GKE = ("principal://iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/"
       "example-project.svc.id.goog/subject/ns/demo-chat/sa/demo-chat")
PRINCIPAL = {"aws": AWS_ROLE, "gcp": GKE}
ENDPOINT = {"aws": "https://s3.us-east-1.amazonaws.com", "gcp": "https://storage.googleapis.com"}


def target(kind, **kw):
    return CloudPolicyTarget(kind, ENDPOINT[kind], "t31/probe.txt", APP_BUCKET, REHEARSAL,
                             (("demo-chat", "demo-chat", PRINCIPAL[kind]),), **kw)


def closed_render(kind, bucket=REHEARSAL, actions=("read", "write", "list", "delete")):
    """The rehearsal-verified policy: A5's canonical render of 'app reads/writes, public denied'."""
    neutral, _ = load_neutral()
    model = neutral.ObjectAccessPolicy(bucket, neutral.SourceRef("aws", APP_BUCKET),
                                       (neutral.Grant(neutral.Workload("demo-chat", "demo-chat"), actions),),
                                       False, False, True)
    return neutral.render_object_policy(kind, model, bucket=bucket,
                                        principal_for=lambda w: PRINCIPAL[kind]).text()


class FakeStore:
    def __init__(self, kind, previous=None, write_error=None, readback=None, authenticated=("ALLOWED",)):
        self.kind, self.policies, self.write_error, self.readback = kind, {APP_BUCKET: previous}, write_error, readback
        self.writes, self.authenticated = [], list(authenticated)

    def read(self, bucket):
        if self.writes and self.readback is not None:
            return self.readback
        return self.policies.get(bucket)

    def write(self, bucket, text):
        self.writes.append((bucket, text))
        if self.write_error:
            raise self.write_error
        self.policies[bucket] = text

    def authenticated_get(self, bucket, key):
        return self.authenticated.pop(0) if len(self.authenticated) > 1 else self.authenticated[0]


def hooks(anonymous="DENIED"):
    clock = iter(range(0, 10_000, 10))
    return {"anonymous_probe": lambda: {"anonymous_get": anonymous}, "sleep": lambda _: None,
            "monotonic": lambda: next(clock), "probe_window": 30}


def claim_for(approved, before="{}\n"):
    sources = {POLICY_FILE: before}
    proposal = {"schema_version": 1, "run_id": "run-bucket-1", "source_sha": "2" * 40, "rationale": "deny anonymous reads",
                "risk": "low", "edits": [{"path": POLICY_FILE, "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
                                          "replacement": approved}]}
    claim = {"application_id": "pol-0001", "approval_id": "approval-pol-1", "verification_id": "verify-pol-1",
             "rehearsal_id": "run-bucket-1", "claimed_at": "2026-11-06T08:55:00Z",
             "binding": {"plan_id": "plan-demo", "plan_digest": "c" * 64,
                         "diff_hash": diff_hash(render_diff(sources, proposal["edits"])), "image_digest": IMAGE,
                         "target_environment_id": "cloud-dev", "expires_at": "2026-11-06T10:00:00Z",
                         "change_kind": "bucket_policy", "policy_digest": policy_digest(approved)}}
    return claim, proposal, sources


def run(kind, tmp_path, store, approved=None, policy_target=None, **kw):
    approved = approved or closed_render(kind)
    claim, proposal, sources = claim_for(approved)
    return apply("pol-0001", claim, proposal, kube=FakeKube(IMAGE), receipts_dir=tmp_path / "r",
                 target_environment_id="cloud-dev", policy_target=policy_target or target(kind), sources=sources,
                 now=NOW, policy_stores={kind: store}, policy_hooks=kw.pop("policy_hooks", hooks()), **kw)


# --- selection and recheck --------------------------------------------------------------------------

def test_the_target_kind_selects_the_applier():
    onprem = PolicyTarget(namespace="target-dev", endpoint="http://seaweedfs:8333", probe_key="k")
    assert isinstance(policy_applier(onprem), OnpremApplier)
    applier = policy_applier(target("aws"), {"aws": FakeStore("aws")})
    assert isinstance(applier, CloudApplier) and applier.kind == "aws"
    with pytest.raises(PolicyRefused, match="TARGET_APPLIER_UNAVAILABLE"):
        policy_applier(target("gcp"), {"aws": FakeStore("aws")})


def test_recheck_tests_are_chosen_by_change_and_target_kind():
    assert recheck_tests("app_image", "onprem") == GATE_RECHECK_TESTS == ["SEC-01.1", "IAM-01.1", "ENC-01.1"]
    assert recheck_tests("bucket_policy", "onprem") == POLICY_GATE_RECHECK_TESTS == ["IAM-01.1"]
    assert recheck_tests("bucket_policy", "gcp") == ["IAM-01.1"]
    with pytest.raises(PolicyRefused):
        recheck_tests("bucket_policy", "azure")


def test_the_neutral_package_is_a5s_loaded_by_path_with_a_recorded_digest():
    neutral, sha = load_neutral()
    assert neutral.__name__ == "bravo_apply._neutral" and len(sha) == 64
    assert neutral.KINDS == ("aws", "gcp", "onprem")


# --- cloud apply --------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["aws", "gcp"])
def test_cloud_policy_is_applied_only_as_the_canonical_render_then_probed(kind, tmp_path):
    store = FakeStore(kind, previous='{"old": true}')
    result = run(kind, tmp_path, store)
    assert result.outcome == "APPLIED", result.steps
    validate_outcome(result.as_dict())
    ((bucket, text),) = store.writes
    assert bucket == APP_BUCKET and REHEARSAL not in text
    assert text == closed_render(kind, bucket=APP_BUCKET)
    assert result.policy["applied_policy_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert result.policy["previous_policy_sha256"] == hashlib.sha256(b'{"old": true}').hexdigest()
    assert result.policy["renderer_sha256"] == load_neutral()[1]
    assert result.gate_recheck["tests"] == ["IAM-01.1"] and result.gate_recheck["target_kind"] == kind
    assert result.target["target_kind"] == kind and result.deployed_digest is None


@pytest.mark.parametrize("kind", ["aws", "gcp"])
def test_a_policy_that_is_not_the_canonical_render_is_refused_before_anything(kind, tmp_path):
    store = FakeStore(kind)
    approved = json.dumps(json.loads(closed_render(kind)))  # same meaning, other bytes
    result = run(kind, tmp_path, store, approved=approved)
    assert (result.outcome, result.reason) == ("REFUSED", "POLICY_NOT_CANONICAL_RENDER")
    assert store.writes == [] and result.cluster_mutation_attempted is False


@pytest.mark.parametrize("kind", ["aws", "gcp"])
def test_public_or_unknown_principals_are_refused(kind, tmp_path):
    neutral, _ = load_neutral()
    public = neutral.ObjectAccessPolicy(REHEARSAL, neutral.SourceRef("aws", APP_BUCKET),
                                        (neutral.Grant(neutral.Anonymous(), ("read",)),), True, False, True)
    approved = neutral.render_object_policy(kind, public, bucket=REHEARSAL).text()
    assert run(kind, tmp_path / "a", FakeStore(kind), approved=approved).reason == "POLICY_GRANTS_ANONYMOUS_READ"
    stranger = target(kind)
    stranger = CloudPolicyTarget(kind, stranger.endpoint, stranger.probe_key, APP_BUCKET, REHEARSAL, ())
    result = run(kind, tmp_path / "b", FakeStore(kind), policy_target=stranger)
    assert result.reason in ("POLICY_PRINCIPAL_UNRESOLVED", "POLICY_NOT_CANONICAL_RENDER")
    assert result.outcome == "REFUSED"


@pytest.mark.parametrize("kind, kw, anonymous, reason", [
    ("aws", {"write_error": RuntimeError("throttled")}, "DENIED", "POLICY_WRITE_FAILED"),
    ("aws", {}, "ALLOWED", "ANONYMOUS_READ_STILL_ALLOWED"),
    ("gcp", {"authenticated": ("DENIED",)}, "DENIED", "AUTHENTICATED_READ_FAILED"),
    ("gcp", {}, "UNKNOWN:503", "PROBE_UNKNOWN"),
    ("aws", {"readback": '{"Version": "2012-10-17", "Statement": []}'}, "DENIED", "POLICY_READBACK_MISMATCH"),
])
def test_after_the_write_every_failure_is_safe_hold(kind, kw, anonymous, reason, tmp_path):
    result = run(kind, tmp_path, FakeStore(kind, **kw), policy_hooks=hooks(anonymous))
    assert (result.outcome, result.reason) == ("SAFE_HOLD", reason)
    assert result.cluster_mutation_attempted is True
    validate_outcome(result.as_dict())


def test_a_store_read_failure_before_the_write_is_a_refusal(tmp_path):
    class Broken(FakeStore):
        def read(self, bucket):
            raise PermissionError("denied")

    result = run("aws", tmp_path, Broken("aws"))
    assert (result.outcome, result.reason, result.cluster_mutation_attempted) == ("REFUSED", "CLUSTER_READ_FAILED", False)


def test_gcs_merge_keeps_unmanaged_members_and_drops_the_public():
    current = [{"role": "roles/storage.legacyBucketOwner", "members": ["projectOwner:example-project"]},
               {"role": "roles/storage.objectViewer", "members": ["allUsers", GKE]}]
    rendered = [{"role": "roles/storage.objectUser", "members": [GKE]}]
    merged = merge_gcs_bindings(current, rendered, {GKE, "allUsers", "allAuthenticatedUsers"})
    assert merged == [current[0], rendered[0]]


def test_anonymous_probe_reads_one_object_without_credentials():
    seen = {}

    class Response:
        status = 403

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b""

    def opener(request, timeout):
        seen["url"], seen["auth"] = request.full_url, request.get_header("Authorization")
        return Response()

    assert anonymous_get("https://storage.googleapis.com", APP_BUCKET, "a b/c", opener=opener) == {"anonymous_get": "DENIED"}
    assert seen == {"url": f"https://storage.googleapis.com/{APP_BUCKET}/a%20b/c", "auth": None}
