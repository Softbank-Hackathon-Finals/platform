"""Fail real nested pytest runs; inspect complete diagnostics and owned Docker residue."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def failure_details(path, nested_exit):
    """A closed vocabulary; never forward nested stdout, stderr or file values."""
    __tracebackhide__ = True
    phases = {"create_started", "create_failed", "create_raised", "fault_injected"}
    codes = {"DAEMON_UNAVAILABLE", "STORAGE_EXHAUSTED", "REGISTRY_RATE_LIMITED",
             "IMAGE_ACCESS_DENIED", "PORT_CONFLICT", "IMAGE_UNAVAILABLE",
             "UNCLASSIFIED_FAILURE", "TIMEOUT", "PROCESS_UNAVAILABLE", "OUTPUT_UNREADABLE"}
    try:
        with path.open("rb") as stream:
            raw = stream.read(513)
        if len(raw) > 512:
            raise ValueError("oversized diagnostic")
        record = json.loads(raw)
        phase = record.get("phase")
        code = record.get("code")
        status = record.get("exit")
        phase = phase if isinstance(phase, str) and phase in phases else "unavailable"
        code = code if isinstance(code, str) and code in codes else "unavailable"
        status = status if type(status) is int and -255 <= status <= 255 else "unavailable"
    except (OSError, ValueError, AttributeError):
        phase = code = status = "unavailable"
    nested_exit = nested_exit if type(nested_exit) is int and -255 <= nested_exit <= 255 else "unavailable"
    return f"phase={phase};code={code};create_exit={status};nested_exit={nested_exit}"


PLUGIN = '''
import json
import os
import time
from pathlib import Path
import subprocess
import secrets
import fixture_process

def pytest_configure(config):
    original = subprocess.run
    init = fixture_process.OwnedDocker.__init__
    def owned(self):
        init(self)
        Path(os.environ["FIXTURE_OWNER_FILE"]).write_text(self.owner)
    fixture_process.OwnedDocker.__init__ = owned
    original_ids = fixture_process.OwnedDocker._ids
    def exit_after_list(self, kind):
        __tracebackhide__ = True
        ids = original_ids(self, kind)
        if kind == "container" and ids and os.environ["FIXTURE_FAULT"] == "container_exit_after_list":
            for identity in ids:
                labels = json.loads(fixture_process.run("docker", "container", "inspect", "--format",
                                    "{{json .Config.Labels}}", identity))
                if labels.get(self.label_key) != self.owner:
                    raise RuntimeError("fault_injection_ownership_unconfirmed")
                auto_remove = fixture_process.run("docker", "container", "inspect", "--format",
                                                  "{{.HostConfig.AutoRemove}}", identity) == "true"
                fixture_process.run("docker", "container", "stop", "--time", "0", identity)
                if auto_remove:
                    deadline = time.monotonic() + 10
                    while identity in original_ids(self, kind):
                        if time.monotonic() >= deadline:
                            raise RuntimeError("fault_injection_exit_unconfirmed")
                        time.sleep(0.05)
            Path(os.environ["FIXTURE_EXIT_FILE"]).write_text("stopped_after_list")
        return ids
    fixture_process.OwnedDocker._ids = exit_after_list
    secrets.token_urlsafe = lambda n: os.environ["FIXTURE_PRIVATE_SENTINEL"]
    def diagnostic(phase, code=None, status=None):
        Path(os.environ["FIXTURE_DIAGNOSTIC_FILE"]).write_text(json.dumps(
            {"phase": phase, "code": code, "exit": status}))
    def injected(args, *a, **kw):
        __tracebackhide__ = True
        kind = os.environ["FIXTURE_FAULT"]
        create = list(args[:2]) == ["docker", "run"]
        network = list(args[:3]) == ["docker", "network", "create"]
        target = ((create and kind in {"exit125", "container_reply", "container_exit_after_list"})
                  or (network and kind == "network_reply"))
        if target:
            diagnostic("create_started")
        if create and kind == "exit125":
            diagnostic("fault_injected", "DAEMON_UNAVAILABLE", 125)
            return subprocess.CompletedProcess(args, 125, "", "Cannot connect to the Docker daemon")
        try:
            result = original(args, *a, **kw)
        except (subprocess.TimeoutExpired, UnicodeError, OSError) as error:
            if target:
                code = ("TIMEOUT" if isinstance(error, subprocess.TimeoutExpired) else
                        "OUTPUT_UNREADABLE" if isinstance(error, UnicodeError) else "PROCESS_UNAVAILABLE")
                diagnostic("create_raised", code)
            raise
        if target and result.returncode:
            diagnostic("create_failed", fixture_process.failure_code(result.stderr), result.returncode)
        if result.returncode == 0 and ((create and kind in {"container_reply", "container_exit_after_list"}) or (network and kind == "network_reply")):
            diagnostic("fault_injected", "OUTPUT_UNREADABLE", 0)
            raise UnicodeDecodeError("utf8", b"\\xff", 0, 1, "injected lost create response")
        return result
    subprocess.run = injected
'''


@pytest.mark.parametrize("showlocals", [False, True])
@pytest.mark.parametrize("fixture,fault", [
    ("single", "exit125"), ("pair", "exit125"),
    ("single", "container_reply"), ("pair", "container_reply"),
    ("pair", "network_reply"),
    ("single", "container_exit_after_list"), ("pair", "container_exit_after_list"),
])
def test_failed_pytest_redacts_secrets_and_cleans_lost_creates(tmp_path, showlocals, fixture, fault):
    __tracebackhide__ = True
    plugin = tmp_path / "fault_plugin.py"
    plugin.write_text(PLUGIN)
    owner_file = tmp_path / "owner"
    diagnostic_file = tmp_path / "diagnostic.json"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), str(Path(__file__).parent)]),
               FIXTURE_OWNER_FILE=str(owner_file), FIXTURE_FAULT=fault,
               FIXTURE_DIAGNOSTIC_FILE=str(diagnostic_file),
               FIXTURE_EXIT_FILE=str(tmp_path / "exit-observed"),
               FIXTURE_PRIVATE_SENTINEL="private-fixture-" + os.urandom(16).hex())
    target = "tests/test_t13_capture.py::test_actual_tls_schema_match_then_mismatch_blocks" if fixture == "single" else (
        "tests/test_t13_two_servers.py::test_two_tls_servers_replication_loss_and_sequence_readback")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "fault_plugin", target,
                             *(["--showlocals"] if showlocals else [])],
                            env=env, capture_output=True, text=True, timeout=120)
    output = result.stdout + result.stderr
    details = failure_details(diagnostic_file, result.returncode)
    if result.returncode != 1:
        pytest.fail("nested_failure_not_observed;" + details, pytrace=False)
    if env["FIXTURE_PRIVATE_SENTINEL"] in output:
        pytest.fail("nested_failure_exposed_fixture_credential", pytrace=False)
    code = "DAEMON_UNAVAILABLE" if fault == "exit125" else "OUTPUT_UNREADABLE"
    if code not in output:
        pytest.fail("original_diagnostic_not_observed;" + details, pytrace=False)
    if "phase=fault_injected;" not in details:
        pytest.fail("fault_injection_not_confirmed;" + details, pytrace=False)
    if "CLEANUP_UNCONFIRMED" in output:
        pytest.fail("nested_cleanup_unconfirmed;" + details, pytrace=False)
    if fault == "container_exit_after_list":
        if not (tmp_path / "exit-observed").exists():
            pytest.fail("fixture_exit_after_list_not_observed", pytrace=False)
    owner = owner_file.read_text()
    for kind in ("container", "network"):
        found = subprocess.run(["docker", kind, "ls", "--quiet", "--no-trunc",
                                *(["--all"] if kind == "container" else []),
                                "--filter", "label=bravo.test-fixture=" + owner],
                               check=True, capture_output=True, text=True, timeout=30)
        assert found.stdout.strip() == ""


def test_cleanup_refuses_changed_ownership():
    from unittest.mock import patch

    from fixture_process import FixtureCommandError, OwnedDocker
    fixture = OwnedDocker()
    identity = "a" * 64
    with patch("fixture_process.run", side_effect=[identity, json.dumps({fixture.label_key: "foreign"})]) as tool:
        with pytest.raises(FixtureCommandError, match="OWNERSHIP_UNCONFIRMED"):
            fixture.cleanup()
    assert all("rm" not in call.args for call in tool.call_args_list)


@pytest.mark.parametrize("result,expected", [
    ("create_failure", "phase=create_failed;code=IMAGE_UNAVAILABLE;create_exit=125;nested_exit=1"),
    ("timeout", "phase=create_raised;code=TIMEOUT;create_exit=unavailable;nested_exit=1"),
    ("lost_reply", "phase=fault_injected;code=OUTPUT_UNREADABLE;create_exit=0;nested_exit=1"),
])
def test_nested_diagnostic_distinguishes_create_failure_from_injection(tmp_path, monkeypatch, result, expected):
    import secrets

    import fixture_process

    private = "PRIVATE-NESTED-DIAGNOSTIC-SENTINEL"
    path = tmp_path / "diagnostic.json"
    monkeypatch.setenv("FIXTURE_DIAGNOSTIC_FILE", str(path))
    monkeypatch.setenv("FIXTURE_PRIVATE_SENTINEL", private)
    monkeypatch.setenv("FIXTURE_FAULT", "container_reply")
    calls = []
    def original(args, **kwargs):
        calls.append(args)
        if result == "timeout":
            raise subprocess.TimeoutExpired([private], 90, output=private, stderr=private)
        return subprocess.CompletedProcess(args, 125 if result == "create_failure" else 0,
                                           private, "manifest unknown " + private)
    monkeypatch.setattr(subprocess, "run", original)
    # The nested plugin installs wrappers directly; register originals for teardown.
    monkeypatch.setattr(secrets, "token_urlsafe", secrets.token_urlsafe)
    monkeypatch.setattr(fixture_process.OwnedDocker, "__init__", fixture_process.OwnedDocker.__init__)
    monkeypatch.setattr(fixture_process.OwnedDocker, "_ids", fixture_process.OwnedDocker._ids)
    namespace = {}
    exec(PLUGIN, namespace)
    namespace["pytest_configure"](None)
    args = ["docker", "run", "-e", "POSTGRES_PASSWORD"]
    if result == "create_failure":
        assert subprocess.run(args, env={"POSTGRES_PASSWORD": private}).returncode == 125
    else:
        error = subprocess.TimeoutExpired if result == "timeout" else UnicodeDecodeError
        with pytest.raises(error):
            subprocess.run(args, env={"POSTGRES_PASSWORD": private})
    assert failure_details(path, 1) == expected
    assert private not in path.read_text()
    assert len(calls) == 1  # Recording a failure never retries a create.


@pytest.mark.parametrize("raw", [
    b'{"phase":"PRIVATE","code":"PRIVATE","exit":"PRIVATE"}',
    b'{"phase":[],"code":{},"exit":true}',
    b'"PRIVATE"', b'{PRIVATE', b'PRIVATE' * 100,
])
def test_nested_diagnostic_never_forwards_untrusted_values(tmp_path, raw):
    path = tmp_path / "diagnostic.json"
    path.write_bytes(raw)
    assert failure_details(path, "PRIVATE") == (
        "phase=unavailable;code=unavailable;create_exit=unavailable;nested_exit=unavailable")


def test_missing_nested_diagnostic_is_explicit(tmp_path):
    assert failure_details(tmp_path / "missing", 1) == (
        "phase=unavailable;code=unavailable;create_exit=unavailable;nested_exit=1")
