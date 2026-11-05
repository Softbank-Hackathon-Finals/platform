import base64
import copy
import hashlib
import json
import subprocess
import urllib.error
from pathlib import Path
from string import Template

import pytest

from bravo_apply.binding import BindingError, diff_hash, policy_digest, render_diff, verify_claim
from bravo_apply.cli import main
from bravo_apply.deploy import FakeKube, KubectlKube
from bravo_apply.path import apply, get_result, validate_outcome
from bravo_apply.policy import (
    ANNOTATION_APPLICATION,
    ANNOTATION_S3_JSON,
    PolicyRefused,
    PolicyTarget,
    load_t9,
    render_closed,
    signed_get,
    translate_policy,
)
from tests.conftest import BEFORE, IMAGE, NOW

REPO_ROOT = Path(__file__).resolve().parents[3]
BUCKET, REHEARSAL = "demo-chat-attachments", "bravo-rehearsal-objects"
POLICY_FILE = "src/target-policy.json"
SEAWEED = "docker.io/chrislusf/seaweedfs:4.52@sha256:" + "5" * 64
# Placeholder credentials in T9's accepted shape; not real keys.
ENV = {"BRAVO_OBJECT_BOOTSTRAP_ACCESS_KEY": "FAKEBOOTSTRAP00001", "BRAVO_OBJECT_BOOTSTRAP_SECRET_KEY": "fake-bootstrap-" + "0" * 30,
       "BRAVO_OBJECT_CLIENT_ACCESS_KEY": "FAKECLIENT00000001", "BRAVO_OBJECT_CLIENT_SECRET_KEY": "fake-client-" + "1" * 30,
       "BRAVO_OBJECT_REHEARSAL_ACCESS_KEY": "FAKEREHEARSAL00001", "BRAVO_OBJECT_REHEARSAL_SECRET_KEY": "fake-rehearsal-" + "2" * 30}


def statement(effect, *, anonymous_only=True, bucket=REHEARSAL, **extra):
    st = {"Sid": "S", "Effect": effect, "Principal": "*", "Action": "s3:GetObject", "Resource": Template("arn:aws:s3:::${bucket}/*").substitute(bucket=bucket)}
    if anonymous_only:
        st["Condition"] = {"StringEquals": {"aws:PrincipalType": "Anonymous"}}
    return st | extra


def policy_text(*statements):
    """Same serialisation as the rehearsal flow's policies (sorted keys)."""
    return json.dumps({"Version": "2012-10-17", "Statement": list(statements)}, sort_keys=True)


CLOSING = policy_text(statement("Deny"))
SOURCE_PUBLIC = policy_text(statement("Allow", anonymous_only=False))


def s3_json(state):
    t9, _ = load_t9()
    doc = t9.render(state, BUCKET, ENV, acknowledge_open=state == "open-demo", demo_run_id="demo-run-1", rehearsal_bucket=REHEARSAL)
    return (json.dumps(doc, indent=2) + "\n").encode()


def secret_obj(data: bytes):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "target-seaweedfs-auth", "namespace": "target-dev", "resourceVersion": "41",
                         "managedFields": [{"manager": "kubectl"}]},
            "data": {"s3.json": base64.b64encode(data).decode()}}


def fake_kube(state="open-demo", **kw):
    return FakeKube(SEAWEED, secrets={"target-seaweedfs-auth": secret_obj(s3_json(state))}, **kw)


@pytest.fixture
def policy_sources():
    return {POLICY_FILE: SOURCE_PUBLIC}


@pytest.fixture
def policy_proposal():
    return {"schema_version": 1, "run_id": "run-bucket-1", "source_sha": "2" * 40, "rationale": "deny anonymous reads",
            "risk": "low", "edits": [{"path": POLICY_FILE, "before_sha256": hashlib.sha256(SOURCE_PUBLIC.encode()).hexdigest(),
                                      "replacement": CLOSING}]}


@pytest.fixture
def policy_claim(policy_proposal, policy_sources):
    return {"application_id": "pol-0001", "approval_id": "approval-pol-1", "verification_id": "verify-pol-1",
            "rehearsal_id": "run-bucket-1", "claimed_at": "2026-11-06T08:55:00Z",
            "binding": {"plan_id": "plan-demo", "plan_digest": "c" * 64,
                        "diff_hash": diff_hash(render_diff(policy_sources, policy_proposal["edits"])),
                        "image_digest": IMAGE, "target_environment_id": "onprem-dev", "expires_at": "2026-11-06T10:00:00Z",
                        "change_kind": "bucket_policy", "policy_digest": policy_digest(CLOSING)}}


TARGET = PolicyTarget(namespace="target-dev", endpoint="http://seaweedfs.target-dev.svc.cluster.local:8333",
                      probe_key="t31/probe.txt", bucket=BUCKET, policy_bucket=REHEARSAL)


class Probes:
    """Scripted probe answers; the last answer repeats."""

    def __init__(self, anonymous=("DENIED",), authenticated=("ALLOWED",)):
        self.anonymous, self.authenticated = list(anonymous), list(authenticated)
        self.calls = {"anonymous": 0, "authenticated": [], "sleep": 0}

    def hooks(self):
        def anon():
            i = min(self.calls["anonymous"], len(self.anonymous) - 1)
            self.calls["anonymous"] += 1
            return {"anonymous_get": self.anonymous[i]}

        def signed(access, secret):
            i = min(len(self.calls["authenticated"]), len(self.authenticated) - 1)
            self.calls["authenticated"].append(access)
            return {"authenticated_get": self.authenticated[i]}

        clock = iter(range(0, 10_000, 10))
        return {"anonymous_probe": anon, "signed_probe": signed, "sleep": self._sleep, "monotonic": lambda: next(clock),
                "probe_window": 60}

    def _sleep(self, _):
        self.calls["sleep"] += 1


def _apply(claim, proposal, tmp_path, kube, probes=None, sources=None, target=TARGET, **kw):
    probes = probes or Probes()
    return apply(claim["application_id"], claim, proposal, kube=kube, receipts_dir=tmp_path / "receipts",
                 target_environment_id="onprem-dev", namespace="target-dev", policy_target=target,
                 sources=sources if sources is not None else {POLICY_FILE: SOURCE_PUBLIC}, now=NOW,
                 policy_hooks=probes.hooks(), **kw)


# --- translation: approved policy -> T9 exposure state ---------------------------------------------------------------

def test_the_rehearsed_closing_policy_translates_to_closed():
    assert translate_policy(CLOSING, policy_bucket=REHEARSAL) == "closed"
    listed = policy_text(statement("Deny", Action=["s3:GetObject"], Principal={"AWS": ["*"]},
                                   Condition={"StringEquals": {"aws:PrincipalType": ["Anonymous"]}}))
    assert translate_policy(listed, policy_bucket=REHEARSAL) == "closed"


@pytest.mark.parametrize("text, code", [
    (SOURCE_PUBLIC, "POLICY_GRANTS_ANONYMOUS_READ"),
    (policy_text(statement("Allow")), "POLICY_GRANTS_ANONYMOUS_READ"),
    (policy_text(statement("Deny"), statement("Allow", anonymous_only=False)), "POLICY_GRANTS_ANONYMOUS_READ"),
    (policy_text(statement("Deny", anonymous_only=False)), "POLICY_DENIES_AUTHENTICATED_READ"),
    (policy_text(statement("Deny", bucket="other-bucket")), "POLICY_UNTRANSLATABLE"),
    (policy_text(statement("Deny", Action=["s3:GetObject", "s3:PutObject"])), "POLICY_UNTRANSLATABLE"),
    (policy_text(statement("Deny", Principal={"AWS": "arn:aws:iam::123456789012:root"})), "POLICY_UNTRANSLATABLE"),
    (policy_text(statement("Deny", Condition={"IpAddress": {"aws:SourceIp": "10.0.0.0/8"}})), "POLICY_UNTRANSLATABLE"),
    (policy_text({k: v for k, v in statement("Deny").items() if k != "Principal"} | {"NotPrincipal": "*"}), "POLICY_UNTRANSLATABLE"),
    (json.dumps({"Statement": [statement("Deny")]}), "POLICY_UNTRANSLATABLE"),
    (policy_text(), "POLICY_UNTRANSLATABLE"),
    ('{"Version": "2012-10-17", "Version": "2012-10-17", "Statement": []}', "POLICY_UNPARSEABLE"),
    ("Deny anonymous reads", "POLICY_UNPARSEABLE"),
])
def test_anything_outside_the_translatable_subset_is_refused(text, code):
    with pytest.raises(PolicyRefused) as info:
        translate_policy(text, policy_bucket=REHEARSAL)
    assert info.value.code == code


# --- rendering through T9 ----------------------------------------------------------------------------------------------

def test_render_closed_uses_t9_and_only_removes_the_anonymous_identity():
    t9, sha = load_t9()
    assert sha == hashlib.sha256((REPO_ROOT / "infra/envs/target-workloads/object_exposure.py").read_bytes()).hexdigest()
    new, (access, secret) = render_closed(t9, s3_json("open-demo"), BUCKET)
    assert new == s3_json("closed")  # byte-identical to `object_exposure.py render --state closed`
    assert [i["name"] for i in json.loads(new)["identities"]] == ["bootstrap", "client", "rehearsal-object-admin"]
    assert (access, secret) == (ENV["BRAVO_OBJECT_CLIENT_ACCESS_KEY"], ENV["BRAVO_OBJECT_CLIENT_SECRET_KEY"])
    assert render_closed(t9, s3_json("closed"), BUCKET)[0] == s3_json("closed")  # already closed: same bytes


@pytest.mark.parametrize("mutate", [
    lambda d: d["identities"].append({"name": "intruder", "actions": ["Admin"]}),
    lambda d: d["identities"][1].update(actions=["Read:demo-chat-attachments"]),
    lambda d: d["identities"][1].update(credentials=[]),
    lambda d: d["identities"].pop(0),
    lambda d: d.update(extra=True),
])
def test_a_target_t9_would_not_render_is_refused(mutate):
    t9, _ = load_t9()
    doc = json.loads(s3_json("open-demo"))
    mutate(doc)
    with pytest.raises(PolicyRefused, match="TARGET_AUTH_UNRECOGNIZED"):
        render_closed(t9, json.dumps(doc).encode(), BUCKET)
    with pytest.raises(PolicyRefused, match="TARGET_AUTH_UNRECOGNIZED"):
        render_closed(t9, s3_json("open-demo"), "another-bucket")


# --- binding: change kind and policy digest ----------------------------------------------------------------------------

def test_policy_binding_is_verified_and_returns_the_kind(policy_claim, policy_proposal, policy_sources):
    binding = verify_claim(policy_claim, policy_proposal, target_environment_id="onprem-dev", now=NOW, sources=policy_sources)
    assert binding["change_kind"] == "bucket_policy" and binding["policy_digest"] == policy_digest(CLOSING)
    assert binding["image_digest"] == IMAGE and binding["approved_paths"] == [POLICY_FILE]


@pytest.mark.parametrize("mutate, code", [
    (lambda c, p, s: c["binding"].update(policy_digest=policy_digest(CLOSING + " ")), "POLICY_DIGEST_MISMATCH"),
    (lambda c, p, s: c["binding"].pop("policy_digest"), "BINDING_INCOMPLETE"),
    (lambda c, p, s: c["binding"].update(policy_digest="latest"), "BINDING_DIGEST_FORMAT"),
    (lambda c, p, s: c["binding"].update(change_kind="dns_record"), "CHANGE_KIND_UNKNOWN"),
    (lambda c, p, s: c["binding"].pop("change_kind"), "CHANGE_KIND_AMBIGUOUS"),
    (lambda c, p, s: p["edits"][0].update(replacement=CLOSING.replace("Anonymous", "Anonymous ")), "DIFF_HASH_MISMATCH"),
])
def test_policy_binding_deviations_are_refused(policy_claim, policy_proposal, policy_sources, mutate, code):
    mutate(policy_claim, policy_proposal, policy_sources)
    with pytest.raises(BindingError) as info:
        verify_claim(policy_claim, policy_proposal, target_environment_id="onprem-dev", now=NOW, sources=policy_sources)
    assert info.value.code == code


def test_policy_change_needs_sources_and_the_one_policy_file(policy_claim, policy_proposal, policy_sources):
    diff = render_diff(policy_sources, policy_proposal["edits"])
    with pytest.raises(BindingError, match="POLICY_SOURCES_REQUIRED"):
        verify_claim(policy_claim, policy_proposal, target_environment_id="onprem-dev", now=NOW, diff=diff)
    other = copy.deepcopy(policy_proposal)
    other["edits"][0]["path"] = "src/other-policy.json"
    sources = {"src/other-policy.json": SOURCE_PUBLIC}
    policy_claim["binding"]["diff_hash"] = diff_hash(render_diff(sources, other["edits"]))
    with pytest.raises(BindingError, match="POLICY_PATH_MISMATCH"):
        verify_claim(policy_claim, other, target_environment_id="onprem-dev", now=NOW, sources=sources)


# --- apply: the policy branch ------------------------------------------------------------------------------------------

def test_policy_apply_replaces_one_secret_rolls_one_workload_and_never_changes_an_image(policy_claim, policy_proposal, tmp_path):
    kube, probes = fake_kube(), Probes()
    result = _apply(policy_claim, policy_proposal, tmp_path, kube, probes)
    assert (result.outcome, result.reason) == ("APPLIED", "APPLIED_AS_APPROVED")
    assert kube.patches == [] and result.deployed_digest is None and result.previous_image == SEAWEED
    assert len(kube.secret_writes) == 1
    written = kube.secret_writes[0]
    assert "managedFields" not in written["metadata"] and written["metadata"]["resourceVersion"] == "41"
    assert base64.b64decode(written["data"]["s3.json"]) == s3_json("closed")
    applied = hashlib.sha256(s3_json("closed")).hexdigest()
    assert kube.restarts == [{ANNOTATION_S3_JSON: applied, ANNOTATION_APPLICATION: "pol-0001"}]
    assert result.change == {"diff_hash": policy_claim["binding"]["diff_hash"], "image_digest": IMAGE,
                             "change_kind": "bucket_policy", "policy_digest": policy_digest(CLOSING)}
    assert result.policy == {"state": "closed", "renderer_sha256": load_t9()[1],
                             "previous_s3_json_sha256": hashlib.sha256(s3_json("open-demo")).hexdigest(),
                             "applied_s3_json_sha256": applied, "anonymous_get": "DENIED", "authenticated_get": "ALLOWED"}
    assert probes.calls["authenticated"] == [ENV["BRAVO_OBJECT_CLIENT_ACCESS_KEY"]]  # the app identity
    assert result.gate_recheck == {"plan_id": "plan-demo", "application_id": "pol-0001", "image_digest": IMAGE,
                                   "tests": ["IAM-01.1"], "policy_digest": policy_digest(CLOSING)}
    assert [s["step"] for s in result.steps] == ["binding", "policy_translate", "image", "started", "policy_apply", "gate_recheck"]
    assert result.steps[2]["status"] == "skipped" and result.target["kind"] == "bucket_policy"
    validate_outcome(result.as_dict())
    t10 = result.t10_application(allow_fake=True)
    assert t10["deployed_digest"] is None and t10["applied_policy_digest"] == policy_digest(CLOSING)
    assert t10["change"]["change_kind"] == "bucket_policy"
    assert result.t10_application() is None
    serialized = (tmp_path / "receipts/pol-0001.json").read_text()
    assert ENV["BRAVO_OBJECT_CLIENT_SECRET_KEY"] not in serialized and ENV["BRAVO_OBJECT_CLIENT_ACCESS_KEY"] not in serialized


def test_policy_second_call_returns_the_record_and_touches_nothing(policy_claim, policy_proposal, tmp_path):
    first = _apply(policy_claim, policy_proposal, tmp_path, fake_kube())
    kube = fake_kube(fail={m: AssertionError(m) for m in ("get_secret", "current_image", "replace_secret", "restart_workload")})
    probes = Probes()
    again = _apply(policy_claim, policy_proposal, tmp_path, kube, probes)
    assert again.as_dict() == first.as_dict() and kube.secret_writes == [] and kube.restarts == []
    assert probes.calls["anonymous"] == 0 and probes.calls["authenticated"] == []


def test_policy_changed_after_approval_is_refused_before_any_cluster_call(policy_claim, policy_proposal, tmp_path):
    changed = copy.deepcopy(policy_proposal)
    changed["edits"][0]["replacement"] = CLOSING.replace("Anonymous", "AWSAccount")
    kube = fake_kube(fail={"get_secret": AssertionError("no cluster call")})
    result = _apply(policy_claim, changed, tmp_path, kube)
    assert (result.outcome, result.reason, result.change) == ("REFUSED", "DIFF_HASH_MISMATCH", None)
    assert kube.secret_writes == [] and result.cluster_mutation_attempted is False


def test_an_approved_policy_that_would_open_the_bucket_is_refused_before_started(policy_claim, policy_proposal, policy_sources, tmp_path):
    allow = policy_text(statement("Allow"))
    opening = copy.deepcopy(policy_proposal)
    opening["edits"][0]["replacement"] = allow
    policy_claim["binding"]["diff_hash"] = diff_hash(render_diff(policy_sources, opening["edits"]))
    policy_claim["binding"]["policy_digest"] = policy_digest(allow)
    kube = fake_kube(fail={"get_secret": AssertionError("no cluster call")})
    result = _apply(policy_claim, opening, tmp_path, kube)
    assert (result.outcome, result.reason) == ("REFUSED", "POLICY_GRANTS_ANONYMOUS_READ")
    assert kube.secret_writes == [] and result.steps[-1]["step"] == "policy_translate"
    assert result.deadline_at is None  # refused before a STARTED record


def test_translation_refusal_is_recorded_with_the_verified_change(policy_claim, policy_proposal, policy_sources, tmp_path):
    overbroad = policy_text(statement("Deny", anonymous_only=False))
    proposal = copy.deepcopy(policy_proposal)
    proposal["edits"][0]["replacement"] = overbroad
    policy_claim["binding"].update(diff_hash=diff_hash(render_diff(policy_sources, proposal["edits"])),
                                   policy_digest=policy_digest(overbroad))
    kube = fake_kube()
    result = _apply(policy_claim, proposal, tmp_path, kube)
    assert (result.outcome, result.reason) == ("REFUSED", "POLICY_DENIES_AUTHENTICATED_READ")
    assert result.change["policy_digest"] == policy_digest(overbroad) and result.steps[-1]["step"] == "policy_translate"
    assert kube.secret_writes == [] and get_result("pol-0001", tmp_path / "receipts").reason == "POLICY_DENIES_AUTHENTICATED_READ"


@pytest.mark.parametrize("probes, reason, field, observed", [
    (Probes(anonymous=["ALLOWED"]), "ANONYMOUS_READ_STILL_ALLOWED", "anonymous_get", "ALLOWED"),
    (Probes(anonymous=["UNKNOWN:URLError"]), "PROBE_UNKNOWN", "anonymous_get", "UNKNOWN:URLError"),
    (Probes(authenticated=["DENIED"]), "AUTHENTICATED_READ_FAILED", "authenticated_get", "DENIED"),
])
def test_a_store_that_does_not_end_up_as_approved_is_safe_hold_without_rollback(policy_claim, policy_proposal, tmp_path,
                                                                                probes, reason, field, observed):
    kube = fake_kube()
    result = _apply(policy_claim, policy_proposal, tmp_path, kube, probes)
    assert (result.outcome, result.reason) == ("SAFE_HOLD", reason) and result.cluster_mutation_attempted is True
    assert result.policy[field] == observed and result.failure_report()["policy"][field] == observed
    assert len(kube.secret_writes) == 1 and len(kube.restarts) == 1  # no second write, no automatic rollback
    assert base64.b64decode(kube.secrets["target-seaweedfs-auth"]["data"]["s3.json"]) == s3_json("closed")
    validate_outcome(result.as_dict())


def test_probe_retries_while_the_store_restarts(policy_claim, policy_proposal, tmp_path):
    probes = Probes(anonymous=["UNKNOWN:URLError", "UNKNOWN:503", "DENIED"], authenticated=["UNKNOWN:503", "ALLOWED"])
    result = _apply(policy_claim, policy_proposal, tmp_path, fake_kube(), probes)
    assert result.outcome == "APPLIED" and probes.calls["anonymous"] == 3 and len(probes.calls["authenticated"]) == 2
    assert probes.calls["sleep"] == 3


@pytest.mark.parametrize("method, outcome, reason, writes", [
    ("get_secret", "REFUSED", "CLUSTER_READ_FAILED", 0),
    ("current_image", "REFUSED", "CLUSTER_READ_FAILED", 0),
    ("replace_secret", "SAFE_HOLD", "SECRET_REPLACE_FAILED", 1),
    ("restart_workload", "SAFE_HOLD", "RESTART_FAILED", 1),
    ("rollout_status", "SAFE_HOLD", "ROLLOUT_STATUS_FAILED", 1),
])
def test_policy_cluster_errors_become_recorded_outcomes(policy_claim, policy_proposal, tmp_path, method, outcome, reason, writes):
    error = subprocess.CalledProcessError(1, ["kubectl"], stderr="Error from server (Forbidden): secrets is forbidden\n")
    kube = fake_kube(fail={method: error})
    result = _apply(policy_claim, policy_proposal, tmp_path, kube)
    assert (result.outcome, result.reason, len(kube.secret_writes)) == (outcome, reason, writes)
    assert result.cluster_mutation_attempted is (outcome == "SAFE_HOLD")
    assert result.steps[-1]["step"] == "policy_apply" and "Forbidden" in result.steps[-1]["detail"]
    assert get_result("pol-0001", tmp_path / "receipts").as_dict() == result.as_dict()


def test_policy_rollout_not_complete_and_unrecognised_target(policy_claim, policy_proposal, tmp_path):
    result = _apply(policy_claim, policy_proposal, tmp_path, fake_kube(rollout_result=False))
    assert (result.outcome, result.reason) == ("SAFE_HOLD", "ROLLOUT_NOT_COMPLETE")
    doc = json.loads(s3_json("open-demo"))
    doc["identities"].append({"name": "intruder", "actions": ["Admin"]})
    kube = FakeKube(SEAWEED, secrets={"target-seaweedfs-auth": secret_obj(json.dumps(doc).encode())})
    claim = dict(policy_claim, application_id="pol-0002")
    refused = _apply(claim, policy_proposal, tmp_path, kube)
    assert (refused.outcome, refused.reason, kube.secret_writes) == ("REFUSED", "TARGET_AUTH_UNRECOGNIZED", [])
    assert refused.policy["previous_s3_json_sha256"] == hashlib.sha256(json.dumps(doc).encode()).hexdigest()


def test_process_death_after_the_secret_replace_leaves_unknown(policy_claim, policy_proposal, tmp_path):
    class Killed(BaseException):
        pass

    kube = fake_kube(fail={"restart_workload": Killed()})
    with pytest.raises(Killed):
        _apply(policy_claim, policy_proposal, tmp_path, kube)
    record = get_result("pol-0001", tmp_path / "receipts")
    assert record.outcome == "UNKNOWN" and record.change["change_kind"] == "bucket_policy" and record.policy is None
    rerun = fake_kube()
    assert _apply(policy_claim, policy_proposal, tmp_path, rerun).as_dict() == record.as_dict() and rerun.secret_writes == []


def test_kind_selects_the_target(policy_claim, policy_proposal, claim, proposal, tmp_path):
    missing = _apply(policy_claim, policy_proposal, tmp_path, fake_kube(), target=None)
    assert (missing.outcome, missing.reason) == ("REFUSED", "POLICY_TARGET_MISSING")
    # an app image binding never takes the policy branch, even when a policy target is configured
    kube = FakeKube("ghcr.io/example/demo-chat@sha256:" + "0" * 64)
    image = apply("app-0001", claim, proposal, kube=kube, receipts_dir=tmp_path / "receipts", target_environment_id="onprem-dev",
                  namespace="target-dev", deployment="demo-chat", container="app", repository="ghcr.io/example/demo-chat",
                  policy_target=TARGET, sources={"app/db_secret.py": BEFORE}, now=NOW)
    assert image.outcome == "APPLIED" and kube.secret_writes == [] and len(kube.patches) == 1 and image.policy is None
    assert set(image.change) == {"diff_hash", "image_digest"} and "kind" not in image.target
    no_target = apply("app-0002", dict(claim, application_id="app-0002"), proposal, kube=kube, receipts_dir=tmp_path / "receipts",
                      target_environment_id="onprem-dev", policy_target=TARGET, sources={"app/db_secret.py": BEFORE}, now=NOW)
    assert (no_target.outcome, no_target.reason) == ("REFUSED", "IMAGE_TARGET_MISSING")


def test_records_written_as_1_1_stay_readable(tmp_path):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    old = REPO_ROOT / "docs/evidence/t31-apply-entry-2026-11-06/records/kind-a-0001.json"
    (receipts / "kind-a-0001.json").write_bytes(old.read_bytes())
    record = get_result("kind-a-0001", receipts)
    assert record.schema_version == "1.1" and record.policy is None and record.outcome == "APPLIED"


def test_policy_target_rejects_values_the_probe_would_refuse_later():
    for bad in ({"endpoint": "http://x:8333/path"}, {"probe_key": "../x"}, {"bucket": "Bad_Bucket"}, {"policy_bucket": "a"}):
        with pytest.raises(ValueError):
            PolicyTarget(**({"namespace": "n", "endpoint": "http://x:8333", "probe_key": "k"} | bad))


# --- signed probe and kubectl adapter ----------------------------------------------------------------------------------

def test_signed_get_sends_a_sigv4_header_and_maps_statuses():
    seen = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"x"

    def opener(request, timeout):
        seen.append(request)
        return Response()

    from datetime import UTC, datetime
    now = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    assert signed_get("http://127.0.0.1:18333", BUCKET, "t31/a b.txt", "AKEY", "s" * 40, opener=opener, now=now) == {
        "authenticated_get": "ALLOWED"}
    req = seen[0]
    assert req.full_url == "http://127.0.0.1:18333/demo-chat-attachments/t31/a%20b.txt"
    auth = req.get_header("Authorization")
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKEY/20261106/us-east-1/s3/aws4_request, "
                           "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=")
    other = []
    signed_get("http://127.0.0.1:18333", BUCKET, "t31/a b.txt", "AKEY", "t" * 40, opener=lambda r, timeout: other.append(r) or Response(), now=now)
    assert other[0].get_header("Authorization") != auth and "s" * 40 not in auth

    def denied(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    def down(request, timeout):
        raise urllib.error.URLError("refused")

    assert signed_get("http://h:1", BUCKET, "k", "A", "s", opener=denied) == {"authenticated_get": "DENIED"}
    assert signed_get("http://h:1", BUCKET, "k", "A", "s", opener=down) == {"authenticated_get": "UNKNOWN:URLError"}


def test_kubectl_secret_replace_goes_through_stdin_not_argv(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("input")))
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    kube = KubectlKube("/k")
    obj = secret_obj(b"secret-bytes")
    kube.replace_secret("target-dev", obj)
    kube.restart_workload("target-dev", "target-seaweedfs", {ANNOTATION_S3_JSON: "a" * 64})
    assert calls[0][0][-5:] == ["replace", "-f", "-", "-o", "name"] and json.loads(calls[0][1]) == obj
    assert all("c2VjcmV0LWJ5dGVz" not in " ".join(cmd) for cmd, _ in calls)
    assert calls[1][0][5:9] == ["patch", "deployment", "target-seaweedfs", "--type"]
    assert json.loads(calls[1][0][-1]) == {"spec": {"template": {"metadata": {"annotations": {ANNOTATION_S3_JSON: "a" * 64}}}}}


# --- CLI ---------------------------------------------------------------------------------------------------------------

def test_cli_check_and_run_for_a_policy_change(policy_claim, policy_proposal, tmp_path, capsys):
    (tmp_path / "claim.json").write_text(json.dumps(policy_claim))
    (tmp_path / "proposal.json").write_text(json.dumps(policy_proposal))
    (tmp_path / "snap/src").mkdir(parents=True)
    (tmp_path / "snap" / POLICY_FILE).write_text(SOURCE_PUBLIC)
    common = ["--claim", str(tmp_path / "claim.json"), "--proposal", str(tmp_path / "proposal.json"), "--target-env", "onprem-dev",
              "--sources", str(tmp_path / "snap"), "--policy-bucket", REHEARSAL]
    assert main(["check", *common]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["policy_state"] == "closed" and out["change_kind"] == "bucket_policy" and "patch" not in out
    run = ["run", *common, "--application-id", "pol-0001", "--namespace", "target-dev", "--receipts", str(tmp_path / "r"),
           "--kube", "fake"]
    with pytest.raises(SystemExit):
        main([*run, "--object-endpoint", "http://127.0.0.1:18333"])  # --probe-key missing
    with pytest.raises(SystemExit):
        main([*run, "--object-endpoint", "http://127.0.0.1:18333/x", "--probe-key", "k"])
    assert main(run) == 2  # no policy target configured: recorded refusal, not an exception
    assert json.loads(capsys.readouterr().out)["reason"] == "POLICY_TARGET_MISSING"
