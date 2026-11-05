import json

from bravo_apply.cli import main
from bravo_apply.deploy import FakeKube
from bravo_apply.path import apply_approved_change, validate_outcome
from tests.conftest import BEFORE, IMAGE, NOW, REPO


def _run(claim, proposal, tmp_path, kube=None, sources=None, **kw):
    kube = kube or FakeKube(f"{REPO}@sha256:{'0' * 64}")
    sources = sources if sources is not None else {"app/db_secret.py": BEFORE}
    return apply_approved_change(claim, proposal, kube=kube, target_environment_id="onprem-dev", namespace="target-dev",
                                 deployment="demo-chat", container="app", repository=REPO, sources=sources,
                                 receipts_dir=tmp_path / "receipts", now=NOW, **kw)


def test_happy_path_reports_exactly_what_t10_compares(claim, proposal, tmp_path):
    result = _run(claim, proposal, tmp_path)
    assert result.outcome == "APPLIED" and result.deployed_digest == IMAGE and result.gate_recheck["tests"] == ["SEC-01.1", "IAM-01.1", "ENC-01.1"]
    app = result.t10_application()
    assert app == {"change": {"diff_hash": claim["binding"]["diff_hash"], "image_digest": IMAGE}, "verification_id": "verify-0001",
                   "approval_id": "approval-0001", "application_id": "app-0001", "job_passed": True, "deployed_digest": IMAGE}
    validate_outcome(result.as_dict())
    assert (tmp_path / "receipts/app-0001.json").exists()
    assert [s["step"] for s in result.steps] == ["binding", "image", "deploy", "gate_recheck"]


def test_refused_binding_touches_no_cluster(claim, proposal, tmp_path):
    proposal["edits"][0]["replacement"] = "pass\n"
    kube = FakeKube(f"{REPO}@sha256:{'0' * 64}")
    result = _run(claim, proposal, tmp_path, kube)
    assert result.outcome == "REFUSED" and result.reason == "DIFF_HASH_MISMATCH" and kube.patches == []
    assert result.t10_application() is None and result.gate_recheck is None


def test_rollout_mismatch_enters_safe_hold_without_gate_request(claim, proposal, tmp_path):
    kube = FakeKube(f"{REPO}@sha256:{'0' * 64}", drift="sha256:" + "f" * 64)
    result = _run(claim, proposal, tmp_path, kube)
    assert result.outcome == "SAFE_HOLD" and result.reason == "RUNNING_DIGEST_MISMATCH"
    assert result.deployed_digest is None and result.gate_recheck is None and result.job_passed is False
    assert len(kube.patches) == 1  # the patch was attempted once; no automatic rollback


def test_same_application_id_is_applied_at_most_once(claim, proposal, tmp_path):
    first = _run(claim, proposal, tmp_path)
    kube = FakeKube(f"{REPO}@sha256:{'0' * 64}")
    second = _run(claim, proposal, tmp_path, kube)
    assert first.outcome == "APPLIED" and second.outcome == "REFUSED" and second.reason == "ALREADY_APPLIED" and kube.patches == []


def test_cli_check_and_run_with_fake_cluster(claim, proposal, tmp_path, capsys):
    (tmp_path / "claim.json").write_text(json.dumps(claim), encoding="utf-8")
    (tmp_path / "proposal.json").write_text(json.dumps(proposal), encoding="utf-8")
    (tmp_path / "snap/app").mkdir(parents=True)
    (tmp_path / "snap/app/db_secret.py").write_text(BEFORE, encoding="utf-8")
    src = ["--sources", str(tmp_path / "snap")]
    assert main(["check", "--claim", str(tmp_path / "claim.json"), "--proposal", str(tmp_path / "proposal.json"), "--target-env", "onprem-dev", "--repository", REPO, *src]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["patch"]["spec"]["template"]["spec"]["containers"][0]["image"] == f"{REPO}@{IMAGE}"
    rc = main(["run", "--claim", str(tmp_path / "claim.json"), "--proposal", str(tmp_path / "proposal.json"), "--target-env", "onprem-dev",
               "--repository", REPO, "--namespace", "target-dev", "--deployment", "demo-chat", "--receipts", str(tmp_path / "r"), *src])
    assert rc == 0 and json.loads(capsys.readouterr().out)["outcome"] == "APPLIED"
    assert main(["check", "--claim", str(tmp_path / "claim.json"), "--proposal", str(tmp_path / "proposal.json"), "--target-env", "onprem-prod", "--repository", REPO, *src]) == 1
