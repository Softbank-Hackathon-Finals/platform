"""T44 desired state → T31 bucket-policy branch: only a reviewed ``closed`` is applied, once."""

import base64
import json
import stat
import subprocess

import pytest

from bravo_apply.cli import main
from bravo_apply.desired import (
    DesiredRefused,
    application_id_for,
    apply_desired_exposure,
    read_desired,
)
from bravo_apply.policy import PolicyTarget
from tests.test_policy import BUCKET, ENV, REPO_ROOT, Probes, fake_kube

TARGET = PolicyTarget(namespace="target-dev", endpoint="http://seaweedfs.target-dev.svc.cluster.local:8333",
                      probe_key="t31/probe.txt", bucket=BUCKET)


def environment(state, *, bucket=BUCKET, name="target-dev"):
    params = {"state": state, "bucket": bucket} | ({"demo_run_id": "demo-2026-11-08"} if state == "open-demo" else {})
    return {"schema_version": "1", "environment": name,
            "modules": [{"id": "target-object-exposure", "version": "1", "params": params},
                        {"id": "tailnet-egress", "version": "1", "params": {}}]}


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
    return repo


def run(repo, revision, tmp_path, kube=None, probes=None, ref="main", target=TARGET):
    kube = kube or fake_kube("open-demo")
    record = apply_desired_exposure("target-dev", revision, reviewed_ref=ref, repo_root=repo.root, kube=kube,
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
                                      "tests": ["IAM-01.1"], "plan_id": "plan-demo"}
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
        assert read_desired(REPO_ROOT, "target-dev", head, "HEAD", BUCKET)["state"] == "closed"
    except DesiredRefused as refused:
        assert refused.code == "T31_NEVER_OPENS"


def test_cli_refuses_an_opening_revision_without_a_record(repo, tmp_path, capsys):
    code = main(["desired-exposure", "--environment", "target-dev", "--revision", repo.opened, "--reviewed-ref", "main",
                 "--repo-root", str(repo.root), "--kubeconfig", str(tmp_path / "unused"), "--namespace", "target-dev",
                 "--object-endpoint", TARGET.endpoint, "--probe-key", TARGET.probe_key,
                 "--receipts", str(tmp_path / "receipts")])
    assert code == 2
    assert json.loads(capsys.readouterr().out) == {"recorded": False, "reason": "T31_NEVER_OPENS", "status": "REFUSED"}
    assert not (tmp_path / "receipts").exists()
