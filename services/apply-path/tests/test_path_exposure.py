"""ADR-0044: one T31 application deploys the approved image, then closes the bucket the approval's plan names."""

import base64
import copy
import json

from bravo_apply.deploy import FakeKube
from bravo_apply.desired import get_desired_result, planned_application_id
from bravo_apply.path import apply, validate_outcome
from tests.conftest import BEFORE, NOW, REPO
from tests.test_desired import TARGET, repo  # noqa: F401 - repo: the base the agent planned against
from tests.test_policy import Probes, s3_json, secret_obj

OLD = f"{REPO}@sha256:{'0' * 64}"


def kube(state="open-demo"):
    return FakeKube(OLD, secrets={"target-seaweedfs-auth": secret_obj(s3_json(state))})


def run(claim, proposal, tmp_path, repo, *, plan, bound=None, k=None, exposure=True, pull_secret=None):  # noqa: F811
    claim = copy.deepcopy(claim)
    if bound is not False:
        claim["binding"]["iac_plan_digest"] = bound or plan["plan_digest"]
    k = k or kube()
    out = apply(claim["application_id"], claim, proposal, kube=k, receipts_dir=tmp_path / "receipts",
                target_environment_id="onprem-dev", namespace="target-dev", deployment="demo-chat", container="app",
                repository=REPO, sources={"app/db_secret.py": BEFORE}, now=NOW, policy_hooks=Probes().hooks(),
                pull_secret=pull_secret,
                exposure={"environment_id": "target-dev", "iac_plan": plan, "target": TARGET,
                          "base_revision": repo.opened, "repo_root": repo.root} if exposure else None)
    return out, k


def anonymous(k):
    doc = json.loads(base64.b64decode(k.secrets["target-seaweedfs-auth"]["data"]["s3.json"]))
    return any(i["name"] == "anonymous" for i in doc["identities"])


def test_the_image_then_the_approved_close_is_one_application(claim, proposal, tmp_path, repo):  # noqa: F811
    plan = repo.plan
    out, k = run(claim, proposal, tmp_path, repo, plan=plan)
    assert (out.outcome, out.reason) == ("APPLIED", "APPLIED_AS_APPROVED")
    steps = [s["step"] for s in out.steps]
    assert steps.index("exposure_plan") < steps.index("started") < steps.index("deploy") < steps.index("exposure_close")
    assert not anonymous(k)
    closed = get_desired_result(planned_application_id(plan["plan_digest"]), tmp_path / "receipts")
    assert closed["status"] == "APPLIED" and closed["desired"]["iac_plan_digest"] == plan["plan_digest"]
    assert closed["desired"]["base_revision"] == repo.opened
    assert out.finished_at >= closed["finished_at"]  # the post-apply run must start after both
    assert out.gate_recheck["tests"]  # the image recheck request is unchanged
    validate_outcome(out.as_dict())


def test_a_plan_other_than_the_approved_one_is_refused_before_any_cluster_call(claim, proposal, tmp_path,
                                                                               repo):  # noqa: F811
    out, k = run(claim, proposal, tmp_path, repo, plan=repo.plan, bound="f" * 64)
    assert (out.outcome, out.reason) == ("REFUSED", "IAC_PLAN_NOT_APPROVED")
    assert k.patches == [] and k.secret_writes == [] and anonymous(k)


def test_a_bound_plan_without_its_close_or_a_close_without_a_bound_plan_is_refused(claim, proposal, tmp_path,
                                                                                  repo):  # noqa: F811
    missing, k1 = run(claim, proposal, tmp_path / "a", repo, plan=repo.plan, exposure=False)
    unbound, k2 = run(claim, proposal, tmp_path / "b", repo, plan=repo.plan, bound=False)
    for out, k in ((missing, k1), (unbound, k2)):
        assert (out.outcome, out.reason) == ("REFUSED", "EXPOSURE_SCOPE_MISMATCH")
        assert k.patches == [] and k.secret_writes == []


def test_a_close_that_cannot_run_holds_the_application_and_keeps_the_image(claim, proposal, tmp_path,
                                                                           repo):  # noqa: F811
    plan = repo.plan
    unknown = kube()
    doc = json.loads(s3_json("open-demo"))
    doc["identities"].append({"name": "backup-writer", "actions": []})  # not a layout T9 renders
    unknown.secrets["target-seaweedfs-auth"] = secret_obj(json.dumps(doc).encode())
    out, k = run(claim, proposal, tmp_path, repo, plan=plan, k=unknown)
    assert (out.outcome, out.reason) == ("SAFE_HOLD", "EXPOSURE_REFUSED")
    assert out.cluster_mutation_attempted is True and k.patches  # the image was deployed; nothing rolled back
    assert out.job_passed is False and out.deployed_digest is None and out.observed_digests
    validate_outcome(out.as_dict())


def test_a_close_refused_after_the_image_holds_the_application_instead_of_leaving_it_started(
        claim, proposal, tmp_path, repo, monkeypatch):  # noqa: F811
    """#821 review: the second Git read (inside the close) can fail after the image is out."""
    from bravo_apply import desired

    real, calls = desired.read_planned, []

    def second_read_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise desired.DesiredRefused("REVISION_UNKNOWN")
        return real(*args, **kwargs)

    monkeypatch.setattr(desired, "read_planned", second_read_fails)
    out, k = run(claim, proposal, tmp_path, repo, plan=repo.plan)
    assert (out.outcome, out.reason) == ("SAFE_HOLD", "EXPOSURE_REFUSED")
    assert k.patches and anonymous(k)  # the image was deployed; the bucket was not touched
    from bravo_apply.path import _record_path

    assert json.loads(_record_path(tmp_path / "receipts", out.application_id).read_text())["outcome"] == "SAFE_HOLD"
    validate_outcome(out.as_dict())


def test_the_candidate_pull_secret_and_the_close_work_in_the_same_application(claim, proposal, tmp_path,
                                                                             repo):  # noqa: F811
    # #809 (GHCR pull) and ADR-0044 (close after the image) in one T31 application
    out, k = run(claim, proposal, tmp_path, repo, plan=repo.plan, pull_secret="bravo-ghcr-reader")
    assert (out.outcome, out.reason) == ("APPLIED", "APPLIED_AS_APPROVED")
    pod = k.patches[0]["spec"]["template"]["spec"]
    assert pod["imagePullSecrets"] == [{"name": "bravo-ghcr-reader"}]
    assert not anonymous(k) and [s["step"] for s in out.steps][-2:] == ["exposure_close", "gate_recheck"]

