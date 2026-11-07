"""T44 desired state → T31 bucket-policy branch: only a reviewed ``closed`` is applied, once."""

import base64
import copy
import json
import stat
import subprocess

import pytest

from bravo_apply.cli import main
from bravo_apply.desired import (
    DesiredRefused,
    application_id_for,
    apply_desired_exposure,
    apply_planned_exposure,
    load_t44,
    planned_application_id,
    read_desired,
)
from bravo_apply.policy import PolicyTarget
from tests.test_policy import BUCKET, ENV, REPO_ROOT, Probes, fake_kube

TARGET = PolicyTarget(namespace="target-dev", endpoint="http://seaweedfs.target-dev.svc.cluster.local:8333",
                      probe_key="t31/probe.txt", bucket=BUCKET)


# T44's real desired state, so the states and plans below are ones T44 itself accepts.
BASE = json.loads((REPO_ROOT / "infra/iac/environments/target-dev.json").read_text())


def environment(state, *, bucket=BUCKET, name="target-dev"):
    params = {"state": state, "bucket": bucket} | ({"demo_run_id": "demo-2026-11-08"} if state == "open-demo" else {})
    doc = copy.deepcopy(BASE) | {"environment": name}
    next(m for m in doc["modules"] if m["id"] == "target-object-exposure")["params"] = params
    return doc


def iac_plan(current, proposed, origin="agent"):
    return load_t44().plan(environment(current), environment(proposed), origin=origin)


def resealed(plan, **changes):
    body = {k: v for k, v in plan.items() if k != "plan_digest"} | changes
    return body | {"plan_digest": load_t44().digest(body)}


class Repo:
    def __init__(self, root):
        self.root = root
        root.mkdir()
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               "-c", "commit.gpgsign=false", *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, doc, name="target-dev"):
        path = self.root / "infra/iac/environments" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "desired state")
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    repo = Repo(tmp_path / "repo")
    repo.opened = repo.commit(environment("open-demo"))
    repo.closed = repo.commit(environment("closed"))
    repo.plan = iac_plan("open-demo", "closed")  # what the operator ran before staging the change
    return repo


def run(repo, revision, tmp_path, kube=None, probes=None, ref="main", target=TARGET, plan=None):
    kube = kube or fake_kube("open-demo")
    record = apply_desired_exposure("target-dev", revision, reviewed_ref=ref, iac_plan=plan or repo.plan,
                                    repo_root=repo.root, kube=kube,
                                    receipts_dir=tmp_path / "receipts", target=target, plan_id="plan-demo",
                                    policy_hooks=(probes or Probes()).hooks())
    return record, kube


def identities(kube):
    data = kube.secrets["target-seaweedfs-auth"]["data"]["s3.json"]
    return [i["name"] for i in json.loads(base64.b64decode(data))["identities"]]


def test_a_reviewed_closed_state_removes_anonymous_and_requests_the_recheck(repo, tmp_path):
    record, kube = run(repo, repo.closed, tmp_path)
    assert (record["status"], record["reason"]) == ("APPLIED", "DESIRED_STATE_APPLIED")
    assert "anonymous" not in identities(kube) and {"bootstrap", "client"} <= set(identities(kube))
    assert record["gate_recheck"] == {"application_id": application_id_for("target-dev", repo.closed),
                                      "environment_id": "target-dev", "revision": repo.closed,
                                      "iac_plan_digest": repo.plan["plan_digest"], "tests": ["IAM-01.1"],
                                      "plan_id": "plan-demo"}
    assert record["desired"]["state_digest"] == repo.plan["proposed_digest"]
    assert record["desired"]["revision"] == repo.closed and record["policy"]["anonymous_get"] == "DENIED"
    assert kube.restarts[0]["bravo.dev/applied-by"] == record["application_id"]
    receipt = tmp_path / "receipts" / f"{record['application_id']}.json"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert not any(value in receipt.read_text() for value in ENV.values())  # digests only, never keys


def test_the_same_revision_twice_changes_nothing_the_second_time(repo, tmp_path):
    first, kube = run(repo, repo.closed, tmp_path)
    again, _ = run(repo, repo.closed, tmp_path, kube=kube)
    assert again == first and len(kube.secret_writes) == 1 and len(kube.restarts) == 1


def test_a_target_already_closed_is_left_alone_and_still_rechecked(repo, tmp_path):
    record, kube = run(repo, repo.closed, tmp_path, kube=fake_kube("closed"))
    assert (record["status"], record["cluster_mutation_attempted"]) == ("ALREADY_CLOSED", False)
    assert kube.secret_writes == [] and kube.restarts == []
    assert record["gate_recheck"]["tests"] == ["IAM-01.1"]


@pytest.mark.parametrize("case, code", [
    ("opens", "T31_NEVER_OPENS"),
    ("not_reviewed", "REVISION_NOT_REVIEWED"),
    ("superseded", "DESIRED_STATE_SUPERSEDED"),
    ("other_bucket", "DESIRED_BUCKET_MISMATCH"),
    ("no_file", "DESIRED_STATE_UNAVAILABLE"),
    ("short_sha", "REVISION_NOT_FULL_SHA"),
    ("bad_ref", "REVIEWED_REF_INVALID"),
])
def test_git_decides_before_anything_is_recorded_or_touched(repo, tmp_path, case, code):
    revision, ref = repo.closed, "main"
    if case == "opens":
        revision = repo.opened
    elif case == "not_reviewed":
        repo.git("switch", "-q", "-c", "side")
        revision = repo.commit(environment("closed") | {"note": "unreviewed"})
    elif case == "superseded":
        repo.commit(environment("open-demo"))  # the reviewed tip reopened for the next demo run
    elif case == "other_bucket":
        revision = repo.commit(environment("closed", bucket="other-bucket"))
    elif case == "no_file":
        repo.commit(environment("closed", name="source-dev"), name="source-dev")
        repo.git("rm", "-q", "infra/iac/environments/target-dev.json")
        repo.git("commit", "-q", "-m", "drop")
        revision = repo.git("rev-parse", "HEAD")
    elif case == "short_sha":
        revision = repo.closed[:12]
    elif case == "bad_ref":
        ref = "--output=/tmp/x"
    kube = fake_kube("open-demo")
    with pytest.raises(DesiredRefused) as refused:
        run(repo, revision, tmp_path, kube=kube, ref=ref)
    assert refused.value.code == code
    assert not (tmp_path / "receipts").exists() and kube.secret_writes == []


@pytest.mark.parametrize("case, code", [
    ("tampered", "IAC_PLAN_DIGEST_MISMATCH"),
    ("refused", "IAC_PLAN_NOT_ACCEPTED"),
    ("other_state", "IAC_PLAN_NOT_THIS_REVISION"),
    ("no_closing", "IAC_PLAN_DOES_NOT_CLOSE"),
    ("other_environment", "IAC_PLAN_INVALID"),
])
def test_the_t44_plan_must_be_the_one_that_closed_this_revision(repo, tmp_path, case, code):
    plan = {
        "tampered": lambda: repo.plan | {"effects": []},
        "refused": lambda: iac_plan("closed", "open-demo"),  # an agent may not widen: T44 refuses it
        "other_state": lambda: resealed(repo.plan, proposed_digest="0" * 64),
        "no_closing": lambda: iac_plan("closed", "closed"),
        "other_environment": lambda: resealed(repo.plan, environment="source-dev"),
    }[case]()
    kube = fake_kube("open-demo")
    with pytest.raises(DesiredRefused) as refused:
        run(repo, repo.closed, tmp_path, kube=kube, plan=plan)
    assert refused.value.code == code
    assert not (tmp_path / "receipts").exists() and kube.secret_writes == []


def test_anonymous_still_readable_after_the_change_is_a_safe_hold(repo, tmp_path):
    record, kube = run(repo, repo.closed, tmp_path, probes=Probes(anonymous=("ALLOWED",)))
    assert (record["status"], record["reason"]) == ("SAFE_HOLD", "ANONYMOUS_READ_STILL_ALLOWED")
    assert record["cluster_mutation_attempted"] and record["gate_recheck"] is None
    assert len(kube.secret_writes) == 1  # not rolled back: a person looks


def test_an_unreadable_secret_is_a_refusal_with_no_change(repo, tmp_path):
    record, kube = run(repo, repo.closed, tmp_path, kube=fake_kube("open-demo", fail={"get_secret": OSError("x")}))
    assert (record["status"], record["reason"], record["cluster_mutation_attempted"]) == (
        "REFUSED", "CLUSTER_READ_FAILED", False)


def test_the_repository_desired_state_has_the_shape_this_reads():
    """T44's real environment file at HEAD: either accepted, or refused only because it opens."""
    head = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    try:
        plan = load_t44().plan(BASE, environment("closed"), origin="agent")
        assert read_desired(REPO_ROOT, "target-dev", head, "HEAD", BUCKET, plan)["state"] == "closed"
    except DesiredRefused as refused:
        assert refused.code == "T31_NEVER_OPENS"


def test_cli_refuses_an_opening_revision_without_a_record(repo, tmp_path, capsys):
    (tmp_path / "plan.json").write_text(json.dumps(repo.plan))
    code = main(["desired-exposure", "--environment", "target-dev", "--revision", repo.opened, "--reviewed-ref", "main",
                 "--iac-plan", str(tmp_path / "plan.json"),
                 "--repo-root", str(repo.root), "--kubeconfig", str(tmp_path / "unused"), "--namespace", "target-dev",
                 "--object-endpoint", TARGET.endpoint, "--probe-key", TARGET.probe_key,
                 "--receipts", str(tmp_path / "receipts")])
    assert code == 2
    assert json.loads(capsys.readouterr().out) == {"recorded": False, "reason": "T31_NEVER_OPENS", "status": "REFUSED"}
    assert not (tmp_path / "receipts").exists()


# --- ADR-0044: close from the approved agent infra.plan (Git keeps open-demo) ---------------------------------------

def planned(repo, tmp_path, plan=None, approved=None, base=None, kube=None, probes=None):
    plan = plan or iac_plan("open-demo", "closed")
    kube = kube or fake_kube("open-demo")
    record = apply_planned_exposure(plan, approved_iac_plan_digest=approved or plan["plan_digest"],
                                    environment_id="target-dev", base_revision=base or repo.opened,
                                    repo_root=repo.root, kube=kube, receipts_dir=tmp_path / "receipts", target=TARGET,
                                    plan_id="plan-demo", policy_hooks=(probes or Probes()).hooks())
    return record, kube


def test_the_approved_agent_plan_closes_the_bucket_once_and_claims_the_exposure(repo, tmp_path):
    from bravo_apply.policy import ANNOTATION_OWNER

    plan = iac_plan("open-demo", "closed")
    record, kube = planned(repo, tmp_path, plan)
    assert (record["status"], record["reason"]) == ("APPLIED", "PLANNED_CLOSE_APPLIED")
    assert record["application_id"] == planned_application_id(plan["plan_digest"]) == "iac-" + plan["plan_digest"][:16]
    assert "anonymous" not in identities(kube)
    owner = kube.secrets["target-seaweedfs-auth"]["metadata"]["annotations"][ANNOTATION_OWNER]
    assert owner == "t31/" + record["application_id"]  # #709: object-operations will not reopen it
    assert record["gate_recheck"] == {"application_id": record["application_id"], "environment_id": "target-dev",
                                      "base_revision": repo.opened, "iac_plan_digest": plan["plan_digest"],
                                      "tests": ["IAM-01.1"], "plan_id": "plan-demo"}
    assert record["desired"]["base_digest"] == plan["base_digest"] and record["desired"]["origin"] == "agent"
    again, _ = planned(repo, tmp_path, plan, kube=kube)
    assert again == record and len(kube.secret_writes) == 1


def _other_change():
    """The closing change plus another module change (an operator may change the module set)."""
    base, proposed = environment("open-demo"), environment("closed")
    proposed["modules"] = [m for m in proposed["modules"] if m["id"] != "tailnet-ingress"]
    return load_t44().plan(base, proposed, origin="operator")


@pytest.mark.parametrize("case, code", [
    ("not_approved", "IAC_PLAN_NOT_APPROVED"),
    ("tampered", "IAC_PLAN_DIGEST_MISMATCH"),
    ("resealed", "IAC_PLAN_NOT_A_CLOSE"),
    ("more_than_close", "IAC_PLAN_NOT_A_CLOSE"),
    ("other_base", "IAC_PLAN_NOT_THIS_BASE"),
    ("base_closed", "PLANNED_BASE_NOT_OPEN"),
    ("no_origin", "IAC_PLAN_INVALID"),
    ("short_base", "REVISION_NOT_FULL_SHA"),
])
def test_only_the_approved_plan_that_only_closes_this_bucket_is_applied(repo, tmp_path, case, code):
    plan, approved, base = iac_plan("open-demo", "closed"), None, None
    if case == "not_approved":
        approved = "0" * 64
    elif case == "tampered":
        plan = plan | {"changes": []}
    elif case == "resealed":
        plan = resealed(plan, changes=[])
    elif case == "more_than_close":
        plan = _other_change()
    elif case == "other_base":
        other = environment("open-demo")
        next(m for m in other["modules"] if m["id"] == "target-object-exposure")["params"]["demo_run_id"] = "demo-other"
        plan = load_t44().plan(other, environment("closed"), origin="agent")
    elif case == "base_closed":
        base = repo.closed
    elif case == "no_origin":
        plan = resealed(plan, origin="someone")
    elif case == "short_base":
        base = repo.opened[:12]
    kube = fake_kube("open-demo")
    with pytest.raises(DesiredRefused) as refused:
        planned(repo, tmp_path, plan, approved=approved, base=base, kube=kube)
    assert refused.value.code == code
    assert not (tmp_path / "receipts").exists() and kube.secret_writes == []


def test_cli_planned_exposure_refuses_an_unapproved_plan_without_a_record(repo, tmp_path, capsys):
    (tmp_path / "plan.json").write_text(json.dumps(iac_plan("open-demo", "closed")))
    code = main(["planned-exposure", "--iac-plan", str(tmp_path / "plan.json"), "--approved-iac-plan-digest", "0" * 64,
                 "--environment", "target-dev", "--base-revision", repo.opened, "--repo-root", str(repo.root),
                 "--kubeconfig", str(tmp_path / "unused"), "--namespace", "target-dev",
                 "--object-endpoint", TARGET.endpoint, "--probe-key", TARGET.probe_key,
                 "--receipts", str(tmp_path / "receipts")])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "IAC_PLAN_NOT_APPROVED"


def test_a_missing_t9_renderer_is_a_recorded_refusal_not_an_exception(repo, tmp_path, monkeypatch):
    """ADR-0044 calls this after the image is deployed: once the record exists, nothing may raise."""
    from bravo_apply import desired
    from bravo_apply.policy import PolicyRefused

    def unavailable():
        raise PolicyRefused("T9_RENDERER_UNAVAILABLE")

    monkeypatch.setattr(desired, "load_t9", unavailable)
    record, kube = planned(repo, tmp_path)
    assert (record["status"], record["reason"], record["cluster_mutation_attempted"]) == (
        "REFUSED", "T9_RENDERER_UNAVAILABLE", False)
    assert kube.secret_writes == [] and record["gate_recheck"] is None
