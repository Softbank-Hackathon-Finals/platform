import hashlib
import json
import subprocess

import pytest

from bravo_apply.binding import (
    BindingError,
    diff_hash,
    diff_paths,
    file_diff,
    render_diff,
    verify_claim,
)
from bravo_apply.deploy import (
    DeployMismatch,
    FakeKube,
    KubectlKube,
    apply_pinned_image,
    image_patch,
)
from tests.conftest import AFTER, BEFORE, IMAGE, NOW, REPO


def test_file_diff_matches_the_rehearsal_runner_format():
    diff = file_diff("app/db_secret.py", BEFORE, AFTER).decode()
    assert diff.startswith("--- a/app/db_secret.py\n+++ b/app/db_secret.py\n@@ ")
    assert "+import json\n" in diff and "-import boto3\n" in diff
    tail = file_diff("x.txt", "a\nb", "a\nc").decode()
    assert tail.endswith("+c\n\\ No newline at end of file\n")  # missing final newline is preserved


def test_render_diff_is_sorted_by_path_and_hash_is_content_sensitive(proposal, sources):
    two = proposal["edits"] + [{"path": "app/config.py", "before_sha256": hashlib.sha256(b"X = 0\n").hexdigest(), "replacement": "X = 1\n"}]
    srcs = dict(sources, **{"app/config.py": "X = 0\n"})
    assert render_diff(srcs, two) == render_diff(srcs, list(reversed(two)))
    assert diff_paths(render_diff(srcs, two)) == ["app/config.py", "app/db_secret.py"]
    a = diff_hash(render_diff(sources, proposal["edits"]))
    assert a != diff_hash(render_diff(sources, [dict(proposal["edits"][0], replacement="pass\n")])) and len(a) == 71


def test_verify_claim_accepts_the_approved_pair_from_sources_or_diff_bytes(claim, proposal, sources):
    binding = verify_claim(claim, proposal, target_environment_id="onprem-dev", now=NOW, sources=sources)
    assert binding["image_digest"] == IMAGE and binding["approved_paths"] == ["app/db_secret.py"]
    handed_off = render_diff(sources, proposal["edits"])
    assert verify_claim(claim, proposal, target_environment_id="onprem-dev", now=NOW, diff=handed_off)["diff_hash"] == claim["binding"]["diff_hash"]
    assert verify_claim(claim, proposal, target_environment_id="onprem-dev", now=NOW, sources=sources, diff=handed_off)


@pytest.mark.parametrize("mutate, code", [
    (lambda c, p, s: p["edits"][0].update(replacement="pass\n"), "DIFF_HASH_MISMATCH"),
    (lambda c, p, s: (p["edits"].append({"path": "app/extra.py", "before_sha256": hashlib.sha256(b"").hexdigest(), "replacement": "X=1\n"}), s.update({"app/extra.py": ""})), "DIFF_HASH_MISMATCH"),
    (lambda c, p, s: s.update({"app/db_secret.py": BEFORE + "# drift\n"}), "BASE_DIGEST_MISMATCH"),
    (lambda c, p, s: s.clear(), "SOURCE_MISSING"),
    (lambda c, p, s: c["binding"].update(expires_at="2026-11-06T08:59:59Z"), "APPROVAL_EXPIRED"),
    (lambda c, p, s: c["binding"].update(target_environment_id="onprem-prod"), "TARGET_ENVIRONMENT_MISMATCH"),
    (lambda c, p, s: c["binding"].update(image_digest="latest"), "BINDING_DIGEST_FORMAT"),
    (lambda c, p, s: c.pop("verification_id"), "CLAIM_INCOMPLETE"),
    (lambda c, p, s: p.update(run_id="run-other"), "PROPOSAL_RUN_MISMATCH"),
    (lambda c, p, s: p.update(edits=[]), "PROPOSAL_SHAPE"),
])
def test_any_deviation_from_the_approved_change_is_refused(claim, proposal, sources, mutate, code):
    mutate(claim, proposal, sources)
    with pytest.raises(BindingError) as info:
        verify_claim(claim, proposal, target_environment_id="onprem-dev", now=NOW, sources=sources)
    assert info.value.code == code


def test_diff_bytes_touching_an_unapproved_file_are_refused(claim, proposal, sources):
    extra = render_diff(sources, proposal["edits"]) + file_diff("app/other.py", "", "X=1\n")
    claim["binding"]["diff_hash"] = diff_hash(extra)
    with pytest.raises(BindingError, match="DIFF_OUTSIDE_APPROVED_PATHS"):
        verify_claim(claim, proposal, target_environment_id="onprem-dev", now=NOW, diff=extra)
    with pytest.raises(BindingError, match="DIFF_SOURCE_REQUIRED"):
        verify_claim(claim, proposal, target_environment_id="onprem-dev", now=NOW)


def test_image_patch_pins_a_digest_never_a_tag():
    patch = image_patch(REPO, IMAGE, "app")
    assert patch["spec"]["template"]["spec"]["containers"] == [{"name": "app", "image": f"{REPO}@{IMAGE}"}]
    with pytest.raises(ValueError):
        image_patch(REPO + ":v1", IMAGE, "app")
    with pytest.raises(ValueError):
        image_patch(REPO, "v1.2.3", "app")


def test_apply_confirms_running_digest(claim):
    kube = FakeKube(f"{REPO}@sha256:{'0' * 64}")
    result = apply_pinned_image(kube, namespace="target-dev", deployment="demo-chat", container="app", repository=REPO, digest=IMAGE)
    assert result["deployed_digest"] == IMAGE and result["pods"] == 2 and kube.patches[0]["spec"]["template"]["spec"]["containers"][0]["image"].endswith(IMAGE)


@pytest.mark.parametrize("kube, code", [
    (FakeKube(f"{REPO}@sha256:{'0' * 64}", rollout_result=False), "ROLLOUT_NOT_COMPLETE"),
    (FakeKube(f"{REPO}@sha256:{'0' * 64}", drift="sha256:" + "f" * 64), "RUNNING_DIGEST_MISMATCH"),
])
def test_apply_raises_when_the_cluster_does_not_run_the_approved_digest(kube, code):
    with pytest.raises(DeployMismatch, match=code) as info:
        apply_pinned_image(kube, namespace="target-dev", deployment="demo-chat", container="app", repository=REPO, digest=IMAGE)
    assert info.value.mutation_attempted is True and info.value.previous_image == f"{REPO}@sha256:{'0' * 64}"


def test_failure_before_the_patch_reports_no_mutation():
    kube = FakeKube(f"{REPO}@sha256:{'0' * 64}", fail={"current_image": RuntimeError("forbidden")})
    with pytest.raises(DeployMismatch) as info:
        apply_pinned_image(kube, namespace="target-dev", deployment="demo-chat", container="app", repository=REPO, digest=IMAGE)
    assert info.value.code == "CLUSTER_READ_FAILED" and info.value.mutation_attempted is False and kube.patches == []


def _completed(stdout):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _pod(phase, image_id, deleting=False):
    return {"metadata": {"deletionTimestamp": "2026-11-06T00:00:00Z"} if deleting else {},
            "status": {"phase": phase, "containerStatuses": [{"name": "app", "imageID": image_id}]}}


def test_kubectl_running_digests_skips_terminating_and_pending_pods(monkeypatch):
    deployment = {"spec": {"selector": {"matchLabels": {"app": "demo-chat"}}}}
    pods = {"items": [_pod("Running", f"docker.io/x@{IMAGE}"), _pod("Running", "docker.io/x@sha256:" + "0" * 64, deleting=True),
                      _pod("Pending", "")]}
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _completed(json.dumps(deployment if "deployment" in cmd else pods))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert KubectlKube("/k").running_digests("ns", "demo-chat", "app") == [IMAGE]
    assert calls[1][-4:] == ["-l", "app=demo-chat", "-o", "json"]


def test_kubectl_rollout_wait_outlasts_kubectl_timeout_and_missing_container_raises(monkeypatch):
    seen = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw["timeout"]))
        if "rollout" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="timed out")
        return _completed(json.dumps({"spec": {"template": {"spec": {"containers": [{"name": "other", "image": "x"}]}}}}))

    monkeypatch.setattr(subprocess, "run", fake_run)
    kube = KubectlKube("/k")
    assert kube.rollout_status("ns", "d", 400) is False and seen[-1][0][-1] == "--timeout=400s" and seen[-1][1] == 430
    with pytest.raises(LookupError):
        kube.current_image("ns", "d", "app")
