import hashlib
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec
from bravo_controls.secret_scan import CONFIG, GitleaksProbe, ScanTarget, _snapshot

ENV = Environment("source", "source", "aws:eks/test")
CTX = RunContext("scan-123", "demo-chat", ENV)
SPEC = Spec("SEC-01.2", "SEC-01", "1", "absent", "not_applicable")


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def setup(tmp_path):
    source, rendered = tmp_path / "source", tmp_path / "rendered"
    source.mkdir()
    rendered.mkdir()
    (source / "app.py").write_text("print('hello')\n")
    (rendered / "app.yaml").write_text("kind: Deployment\n")
    binary = tmp_path / "qualified-binary"
    binary.write_bytes(b"unit-test executable")
    target = ScanTarget("demo-chat", ENV, source, rendered, "a" * 40)
    evidence = []
    probe = GitleaksProbe(
        target,
        executable=binary,
        executable_sha256=digest(binary.read_bytes()),
        evidence_sink=evidence.append,
    )
    return probe, evidence


def clean_run(command, **kwargs):
    if command[-1] == "version":
        return subprocess.CompletedProcess(command, 0, b"8.30.1\n")
    Path(command[command.index("--report-path") + 1]).write_text("[]")
    return subprocess.CompletedProcess(command, 0)


def test_clean_two_scans_bound_and_environment_removed(setup, monkeypatch):
    probe, evidence = setup
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "never-forward-this")
    monkeypatch.setenv("GITLEAKS_CONFIG_TOML", "attacker-config")
    with patch("bravo_controls.secret_scan.subprocess.run", side_effect=clean_run) as run:
        result = probe(CTX, SPEC)
    assert result.observed == "absent"
    assert result.unknown_reason is None
    assert len(run.call_args_list) == 3
    for call in run.call_args_list:
        assert set(call.kwargs["env"]) == {"HOME", "TMPDIR", "LANG", "LC_ALL"}
    for call in run.call_args_list[1:]:
        command = call.args[0]
        assert "--ignore-gitleaks-allow" in command
        assert "--config" in command and "--gitleaks-ignore-path" in command
        assert "--redact=100" in command and "--baseline-path" not in command
    saved = json.loads(evidence[0])
    assert saved["run_id"] == CTX.run_id
    assert saved["snapshots"][0]["scope"] != saved["snapshots"][1]["scope"]
    assert result.output_sha256 == digest(evidence[0])
    assert b"never-forward-this" not in evidence[0]


def test_finding_is_fail_without_copying_secret_or_path(setup):
    probe, evidence = setup

    def finding(command, **kwargs):
        response = clean_run(command, **kwargs)
        if command[-1] != "version":
            Path(command[command.index("--report-path") + 1]).write_text(
                json.dumps(
                    [
                        {
                            "RuleID": "test",
                            "File": "/sensitive/customer/file",
                            "Fingerprint": "fp",
                            "StartLine": 1,
                            "EndLine": 1,
                            "Secret": "do-not-store",
                            "Match": "private context",
                        }
                    ]
                )
            )
            response.returncode = 1
        return response

    with patch("bravo_controls.secret_scan.subprocess.run", side_effect=finding):
        result = probe(CTX, SPEC)
    assert result.observed == "present"
    assert not any(
        value in evidence[0] for value in (b"sensitive", b"do-not-store", b"private context")
    )


def test_timeout_never_returns_clean(setup):
    probe, evidence = setup

    def timeout(command, **kwargs):
        if command[-1] == "version":
            return clean_run(command, **kwargs)
        raise subprocess.TimeoutExpired(command, 65)

    with patch("bravo_controls.secret_scan.subprocess.run", side_effect=timeout):
        with pytest.raises(TimeoutError):
            probe(CTX, SPEC)
    assert evidence == []


@pytest.mark.parametrize("code,report", [(2, "[]"), (1, "[]"), (0, "{bad"), (0, "")])
def test_error_or_malformed_report_unknown(setup, code, report):
    probe, _ = setup

    def bad(command, **kwargs):
        response = clean_run(command, **kwargs)
        if command[-1] != "version":
            Path(command[command.index("--report-path") + 1]).write_text(report)
            response.returncode = code
        return response

    with patch("bravo_controls.secret_scan.subprocess.run", side_effect=bad):
        result = probe(CTX, SPEC)
    assert result.observed is None
    assert result.unknown_reason == "TOOL_ERROR"


def test_digest_drift_rejected_before_execution(setup):
    probe, _ = setup
    probe.executable.write_bytes(b"changed")
    with patch("bravo_controls.secret_scan.subprocess.run") as run:
        with pytest.raises(ValueError, match="digest"):
            probe(CTX, SPEC)
    run.assert_not_called()


def test_wrong_version_rejected(setup):
    probe, _ = setup
    with patch(
        "bravo_controls.secret_scan.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, b"8.29.0"),
    ):
        with pytest.raises(ValueError, match="version"):
            probe(CTX, SPEC)


def test_cross_environment_rejected(setup):
    probe, _ = setup
    with pytest.raises(ValueError, match="scope"):
        probe(RunContext("other", "other-app", ENV), SPEC)


def test_evidence_retention_failure_not_success(setup):
    probe, _ = setup

    def fail(_):
        raise OSError("disk full")

    probe.sink = fail
    with patch("bravo_controls.secret_scan.subprocess.run", side_effect=clean_run):
        with pytest.raises(OSError):
            probe(CTX, SPEC)


@pytest.mark.parametrize("kind", ["empty", "symlink-file", "symlink-dir", "git", "binary", "fifo"])
def test_unsupported_snapshot_fails_closed(tmp_path, kind):
    src = tmp_path / "src"
    src.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside")
    if kind == "symlink-file":
        (src / "link").symlink_to(outside)
    elif kind == "symlink-dir":
        (src / "link").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "git":
        (src / ".git").mkdir()
    elif kind == "binary":
        (src / "binary").write_bytes(b"hello\x00")
    elif kind == "fifo":
        os.mkfifo(src / "pipe")
    with pytest.raises((ValueError, OSError)):
        _snapshot(src, tmp_path / "out")


def test_snapshot_budget_and_manifest_are_content_bound(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x").write_text("first")
    before = _snapshot(src, tmp_path / "out1")
    (src / "x").write_text("other")
    assert before != _snapshot(src, tmp_path / "out2")
    monkeypatch.setattr("bravo_controls.secret_scan.MAX_FILE_BYTES", 2)
    with pytest.raises(ValueError):
        _snapshot(src, tmp_path / "out3")


@pytest.mark.skipif(
    not os.environ.get("BRAVO_TEST_GITLEAKS"), reason="qualified local binary opt-in"
)
def test_real_scanner_ignores_app_suppressions(setup):
    probe, evidence = setup
    path = Path(os.environ["BRAVO_TEST_GITLEAKS"])
    probe.executable = path
    probe.executable_sha256 = os.environ["BRAVO_TEST_GITLEAKS_SHA256"]
    # Synthetic non-working credential, assembled so this test's source does not
    # itself embed a contiguous secret-shaped value.
    canary = "ghp_" + hashlib.sha256(b"bravo synthetic credential fixture").hexdigest()[:36]
    (probe.target.source / "app.py").write_text(f"token = '{canary}' # gitleaks:allow\n")
    (probe.target.source / ".gitleaks.toml").write_text("[allowlist]\nregexes=['.*']\n")
    assert probe(CTX, SPEC).observed == "present"
    assert canary.encode() not in evidence[-1]
    (probe.target.source / "app.py").write_text("print('clean')\n")
    assert probe(CTX, SPEC).observed == "absent"
    assert CONFIG == b"[extend]\nuseDefault = true\n"
