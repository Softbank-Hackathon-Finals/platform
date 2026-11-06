import copy
import json
import subprocess
import threading
import time

import jsonschema
import pytest

from bravo_apply.cli import FAKE_WARNING, main
from bravo_apply.deploy import FakeKube
from bravo_apply.path import (
    RecordUnreadable,
    apply,
    apply_approved_change,
    get_result,
    validate_outcome,
)
from tests.conftest import BEFORE, IMAGE, NOW, REPO

OLD = f"{REPO}@sha256:{'0' * 64}"


def _apply(claim, proposal, tmp_path, kube=None, sources=None, application_id=None, **kw):
    kube = kube or FakeKube(OLD)
    sources = sources if sources is not None else {"app/db_secret.py": BEFORE}
    return apply(claim["application_id"] if application_id is None else application_id, claim, proposal, kube=kube, receipts_dir=tmp_path / "receipts",
                 target_environment_id="onprem-dev", namespace="target-dev", deployment="demo-chat", container="app",
                 repository=REPO, sources=sources, now=NOW, **kw)


def test_happy_path_reports_exactly_what_t10_compares(claim, proposal, tmp_path):
    result = _apply(claim, proposal, tmp_path)
    assert result.outcome == "APPLIED" and result.deployed_digest == IMAGE and result.gate_recheck["tests"] == ["SEC-01.1", "IAM-01.1", "ENC-01.1"]
    assert result.t10_application(allow_fake=True) == {
        "change": {"diff_hash": claim["binding"]["diff_hash"], "image_digest": IMAGE}, "verification_id": "verify-0001",
        "approval_id": "approval-0001", "application_id": "app-0001", "job_passed": True, "deployed_digest": IMAGE}
    assert result.t10_application() is None  # a FakeKube result is never reported to T10 as deployed
    assert result.failure_report() is None and result.cluster == "fake"
    assert result.previous_image == OLD and result.observed_digests == [IMAGE, IMAGE] and result.cluster_mutation_attempted is True
    validate_outcome(result.as_dict())
    assert [s["step"] for s in result.steps] == ["binding", "image", "started", "deploy", "gate_recheck"]
    assert get_result("app-0001", tmp_path / "receipts").as_dict() == result.as_dict()
    assert [p.name for p in (tmp_path / "receipts").iterdir()] == ["app-0001.json"]  # no temp files left


def test_second_call_returns_the_original_outcome_and_never_patches(claim, proposal, tmp_path):
    first = _apply(claim, proposal, tmp_path)
    kube = FakeKube(OLD)
    second = _apply(claim, proposal, tmp_path, kube)
    assert second.as_dict() == first.as_dict() and kube.patches == []


def test_refused_is_recorded_with_its_own_reason_and_a_new_id_still_applies(claim, proposal, tmp_path):
    bad = copy.deepcopy(proposal)
    bad["edits"][0]["replacement"] = "pass\n"
    kube = FakeKube(OLD)
    refused = _apply(claim, bad, tmp_path, kube)
    assert refused.outcome == "REFUSED" and refused.reason == "DIFF_HASH_MISMATCH" and kube.patches == []
    assert refused.change is None and refused.cluster_mutation_attempted is False and refused.gate_recheck is None
    assert refused.t10_application() is None
    report = refused.failure_report()
    assert report["outcome"] == "REFUSED" and report["reason"] == "DIFF_HASH_MISMATCH" and report["observed_digests"] == []
    # the same id keeps its original outcome even with a corrected proposal: no ALREADY_APPLIED, no patch
    again = _apply(claim, proposal, tmp_path, kube)
    assert again.as_dict() == refused.as_dict() and kube.patches == []
    assert get_result("app-0001", tmp_path / "receipts").reason == "DIFF_HASH_MISMATCH"
    # T10 issues a new claim for the corrected change; it applies normally
    claim2 = dict(claim, application_id="app-0002")
    applied = _apply(claim2, proposal, tmp_path, kube)
    assert applied.outcome == "APPLIED" and len(kube.patches) == 1


def test_rollout_mismatch_enters_safe_hold_with_observed_digest(claim, proposal, tmp_path):
    drift = "sha256:" + "f" * 64
    kube = FakeKube(OLD, drift=drift)
    result = _apply(claim, proposal, tmp_path, kube)
    assert result.outcome == "SAFE_HOLD" and result.reason == "RUNNING_DIGEST_MISMATCH"
    assert result.deployed_digest is None and result.gate_recheck is None and result.job_passed is False
    assert len(kube.patches) == 1  # the patch was attempted once; no automatic rollback
    report = result.failure_report()
    assert report["observed_digests"] == [drift, drift] and report["previous_image"] == OLD and report["cluster_mutation_attempted"] is True
    again = _apply(claim, proposal, tmp_path, FakeKube(OLD))
    assert again.as_dict() == result.as_dict()


def test_rollout_not_complete_is_safe_hold(claim, proposal, tmp_path):
    result = _apply(claim, proposal, tmp_path, FakeKube(OLD, rollout_result=False))
    assert result.outcome == "SAFE_HOLD" and result.reason == "ROLLOUT_NOT_COMPLETE" and result.observed_digests == [IMAGE, IMAGE]


def test_process_death_after_patch_leaves_unknown_and_is_never_patched_again(claim, proposal, tmp_path):
    class Killed(BaseException):
        """Stands in for SIGKILL: not an Exception, so nothing in the path may swallow it."""

    kube = FakeKube(OLD, fail={"rollout_status": Killed()})
    with pytest.raises(Killed):
        _apply(claim, proposal, tmp_path, kube)
    assert len(kube.patches) == 1
    record = get_result("app-0001", tmp_path / "receipts")
    assert record.outcome == "UNKNOWN" and record.reason == "APPLY_STARTED_NOT_FINISHED"
    assert record.cluster_mutation_attempted is None and record.finished_at is None and record.deadline_at > record.started_at
    assert record.change == {"diff_hash": claim["binding"]["diff_hash"], "image_digest": IMAGE}
    assert record.failure_report()["outcome"] == "UNKNOWN" and record.t10_application(allow_fake=True) is None
    rerun_kube = FakeKube(OLD)
    rerun = _apply(claim, proposal, tmp_path, rerun_kube)
    assert rerun.as_dict() == record.as_dict() and rerun_kube.patches == []


@pytest.mark.parametrize("method, exc, outcome, reason, patches", [
    ("current_image", subprocess.CalledProcessError(1, ["kubectl"], stderr="Error from server (Forbidden): nope\n"), "REFUSED", "CLUSTER_READ_FAILED", 0),
    ("current_image", subprocess.TimeoutExpired(["kubectl"], 120), "REFUSED", "CLUSTER_READ_FAILED", 0),
    ("patch_image", subprocess.CalledProcessError(1, ["kubectl"], stderr="connection reset\n"), "SAFE_HOLD", "PATCH_FAILED", 1),
    ("rollout_status", subprocess.TimeoutExpired(["kubectl"], 330), "SAFE_HOLD", "ROLLOUT_STATUS_FAILED", 1),
    ("running_digests", subprocess.CalledProcessError(1, ["kubectl"], stderr="boom\n"), "SAFE_HOLD", "RUNNING_DIGEST_UNREADABLE", 1),
])
def test_cluster_errors_become_recorded_outcomes(claim, proposal, tmp_path, method, exc, outcome, reason, patches):
    kube = FakeKube(OLD, fail={method: exc})
    result = _apply(claim, proposal, tmp_path, kube)
    assert (result.outcome, result.reason, len(kube.patches)) == (outcome, reason, patches)
    assert result.cluster_mutation_attempted is (outcome == "SAFE_HOLD")
    assert result.change is not None  # binding was verified before the cluster was called
    assert get_result("app-0001", tmp_path / "receipts").as_dict() == result.as_dict()
    deploy_step = result.steps[-1]
    assert deploy_step["step"] == "deploy" and deploy_step["detail"].startswith(reason)


def test_application_id_must_match_the_claim_and_be_well_formed(claim, proposal, tmp_path):
    result = _apply(claim, proposal, tmp_path, application_id="app-9999")
    assert result.outcome == "REFUSED" and result.reason == "APPLICATION_ID_MISMATCH" and result.application_id == "app-9999"
    for bad in ("../escape", "", "a/b"):
        with pytest.raises(ValueError):
            _apply(claim, proposal, tmp_path, application_id=bad)
        with pytest.raises(ValueError):
            get_result(bad, tmp_path / "receipts")
    assert sorted(p.name for p in (tmp_path / "receipts").iterdir()) == ["app-9999.json"]


def test_get_result_none_before_start_and_corrupt_records_are_not_overwritten(claim, proposal, tmp_path):
    receipts = tmp_path / "receipts"
    assert get_result("app-0001", receipts) is None
    receipts.mkdir()
    (receipts / "app-0001.json").write_text('{"outcome": "APPL', encoding="utf-8")
    with pytest.raises(RecordUnreadable):
        get_result("app-0001", receipts)
    kube = FakeKube(OLD)
    with pytest.raises(RecordUnreadable):
        _apply(claim, proposal, tmp_path, kube)
    assert kube.patches == [] and (receipts / "app-0001.json").read_text(encoding="utf-8") == '{"outcome": "APPL'


def test_concurrent_calls_for_one_id_patch_once(claim, proposal, tmp_path):
    class SlowKube(FakeKube):
        def rollout_status(self, *a):
            time.sleep(0.2)
            return True

    kube = SlowKube(OLD)
    results, start = [], threading.Barrier(6)

    def worker():
        start.wait()
        results.append(_apply(claim, proposal, tmp_path, kube))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(kube.patches) == 1
    assert len(results) == 6 and {r.outcome for r in results} <= {"APPLIED", "UNKNOWN"} and "APPLIED" in {r.outcome for r in results}
    assert get_result("app-0001", tmp_path / "receipts").outcome == "APPLIED"


def test_legacy_entry_point_requires_a_receipts_dir(claim, proposal, tmp_path):
    kw = {"kube": FakeKube(OLD), "target_environment_id": "onprem-dev", "namespace": "target-dev", "deployment": "demo-chat",
          "container": "app", "repository": REPO, "sources": {"app/db_secret.py": BEFORE}, "now": NOW}
    with pytest.raises(TypeError):
        apply_approved_change(claim, proposal, **kw)
    assert apply_approved_change(claim, proposal, receipts_dir=tmp_path / "r", **kw).outcome == "APPLIED"


def test_schema_rejects_inconsistent_records(claim, proposal, tmp_path):
    doc = _apply(claim, proposal, tmp_path).as_dict()
    for mutate in (lambda d: d.update(cluster_mutation_attempted=False), lambda d: d.update(gate_recheck=None),
                   lambda d: d.update(outcome="REFUSED"), lambda d: d.update(outcome="UNKNOWN"),
                   lambda d: d.update(schema_version="1.0"), lambda d: d.update(started_at="2026-11-06 09:00")):
        bad = copy.deepcopy(doc)
        mutate(bad)
        with pytest.raises(jsonschema.ValidationError):
            validate_outcome(bad)


@pytest.fixture
def files(claim, proposal, tmp_path):
    (tmp_path / "claim.json").write_text(json.dumps(claim), encoding="utf-8")
    (tmp_path / "proposal.json").write_text(json.dumps(proposal), encoding="utf-8")
    (tmp_path / "snap/app").mkdir(parents=True)
    (tmp_path / "snap/app/db_secret.py").write_text(BEFORE, encoding="utf-8")
    common = ["--claim", str(tmp_path / "claim.json"), "--proposal", str(tmp_path / "proposal.json"), "--target-env", "onprem-dev",
              "--repository", REPO, "--sources", str(tmp_path / "snap")]
    run = ["run", *common, "--application-id", "app-0001", "--namespace", "target-dev", "--deployment", "demo-chat",
           "--receipts", str(tmp_path / "r")]
    return common, run


def test_cli_check(files, capsys, frozen_now):
    common, _ = files
    assert main(["check", *common]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["patch"]["spec"]["template"]["spec"]["containers"][0]["image"] == f"{REPO}@{IMAGE}"
    assert main(["check", *[a if a != "onprem-dev" else "onprem-prod" for a in common]]) == 1


def test_cli_run_requires_an_explicit_cluster_choice(files, capsys):
    _, run = files
    with pytest.raises(SystemExit) as info:
        main(run)
    assert info.value.code == 2 and "--kube" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main([*run, "--kube", "kubectl"])  # kubectl without --kubeconfig
    with pytest.raises(SystemExit):
        main([*[a if a != "app-0001" else "../x" for a in run], "--kube", "fake"])


def test_cli_fake_run_says_it_is_not_a_real_cluster_and_get_result_matches(files, tmp_path, capsys, frozen_now):
    _, run = files
    assert main([*run, "--kube", "fake"]) == 0
    captured = capsys.readouterr()
    ran = json.loads(captured.out)
    assert ran["outcome"] == "APPLIED" and ran["cluster"] == "fake" and ran["warning"] == FAKE_WARNING
    assert "WARNING FAKE_CLUSTER" in captured.err
    assert main(["get-result", "--application-id", "app-0001", "--receipts", str(tmp_path / "r")]) == 0
    assert json.loads(capsys.readouterr().out) == ran
    assert main(["get-result", "--application-id", "app-0001", "--receipts", str(tmp_path / "r"), "--t10"]) == 0
    t10 = json.loads(capsys.readouterr().out)
    assert t10["application"] is None and t10["failure"] is None  # fake APPLIED is not reported as deployed
    assert main([*run, "--kube", "fake"]) == 0  # second run: same record, no cluster call
    captured = capsys.readouterr()
    assert json.loads(captured.out) == ran and "existing record returned unchanged" in captured.err


def test_cli_get_result_exit_codes(files, claim, tmp_path, capsys):
    assert main(["get-result", "--application-id", "app-0404", "--receipts", str(tmp_path / "r")]) == 5
    assert json.loads(capsys.readouterr().out) == {"application_id": "app-0404", "outcome": None, "reason": "NOT_STARTED"}
    bad = dict(claim, binding=dict(claim["binding"], target_environment_id="onprem-prod"))
    (tmp_path / "claim.json").write_text(json.dumps(bad), encoding="utf-8")
    _, run = files
    assert main([*run, "--kube", "fake"]) == 2
    capsys.readouterr()
    assert main(["get-result", "--application-id", "app-0001", "--receipts", str(tmp_path / "r"), "--t10"]) == 2
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["outcome"] == "REFUSED" and failure["reason"] == "TARGET_ENVIRONMENT_MISMATCH"
    (tmp_path / "r/app-0001.json").write_text("{}", encoding="utf-8")
    assert main(["get-result", "--application-id", "app-0001", "--receipts", str(tmp_path / "r")]) == 6
