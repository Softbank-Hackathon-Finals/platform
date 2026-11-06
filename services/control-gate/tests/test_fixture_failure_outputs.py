"""Fail real nested pytest runs; inspect complete diagnostics and owned Docker residue."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fixture_process import observed_codes

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
    def injected(args, *a, **kw):
        __tracebackhide__ = True
        kind = os.environ["FIXTURE_FAULT"]
        create = list(args[:2]) == ["docker", "run"]
        network = list(args[:3]) == ["docker", "network", "create"]
        if create and kind == "exit125":
            Path(os.environ["FIXTURE_INJECTED_FILE"]).write_text("reached")
            return subprocess.CompletedProcess(args, 125, "", "Cannot connect to the Docker daemon")
        result = original(args, *a, **kw)
        if result.returncode == 0 and ((create and kind in {"container_reply", "container_exit_after_list"}) or (network and kind == "network_reply")):
            Path(os.environ["FIXTURE_INJECTED_FILE"]).write_text("reached")
            raise UnicodeDecodeError("utf8", b"\\xff", 0, 1, "injected lost create response")
        return result
    subprocess.run = injected
'''


def failure_summary(reason, result, injected):
    """Reason plus the nested exit code, whether the fault fired, and fixed fixture codes only.

    Nested stdout/stderr, argv, environment and fixture credentials never enter the message.
    """
    codes = ",".join(observed_codes(result.stdout + result.stderr)) or "none"
    return (f"{reason} nested_exit={int(result.returncode)} injected={'yes' if injected else 'no'} "
            f"fixture_codes={codes}")


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
    injected_file = tmp_path / "fault-injected"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), str(Path(__file__).parent)]),
               FIXTURE_OWNER_FILE=str(owner_file), FIXTURE_FAULT=fault,
               FIXTURE_EXIT_FILE=str(tmp_path / "exit-observed"),
               FIXTURE_INJECTED_FILE=str(injected_file),
               FIXTURE_PRIVATE_SENTINEL="private-fixture-" + os.urandom(16).hex())
    target = "tests/test_t13_capture.py::test_actual_tls_schema_match_then_mismatch_blocks" if fixture == "single" else (
        "tests/test_t13_two_servers.py::test_two_tls_servers_replication_loss_and_sequence_readback")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "fault_plugin", target,
                             *(["--showlocals"] if showlocals else [])],
                            env=env, capture_output=True, text=True, timeout=120)
    output = result.stdout + result.stderr

    def fail(reason):
        __tracebackhide__ = True
        pytest.fail(failure_summary(reason, result, injected_file.exists()), pytrace=False)

    if result.returncode != 1:
        fail("nested_failure_not_observed")
    if env["FIXTURE_PRIVATE_SENTINEL"] in output:
        fail("nested_failure_exposed_fixture_credential")
    code = "DAEMON_UNAVAILABLE" if fault == "exit125" else "OUTPUT_UNREADABLE"
    if code not in output:
        fail("original_diagnostic_not_observed")
    if "CLEANUP_UNCONFIRMED" in output:
        fail("nested_cleanup_unconfirmed")
    if fault == "container_exit_after_list":
        if not (tmp_path / "exit-observed").exists():
            fail("fixture_exit_after_list_not_observed")
    owner = owner_file.read_text()
    for kind in ("container", "network"):
        found = subprocess.run(["docker", kind, "ls", "--quiet", "--no-trunc",
                                *(["--all"] if kind == "container" else []),
                                "--filter", "label=bravo.test-fixture=" + owner],
                               check=True, capture_output=True, text=True, timeout=30)
        assert found.stdout.strip() == ""


@pytest.mark.parametrize("injected", [False, True])
def test_failure_summary_keeps_exit_injection_and_fixed_codes_only(injected):
    sentinel = "private-fixture-" + "ab" * 16
    nested = subprocess.CompletedProcess(
        ["pytest", "-e", "POSTGRES_PASSWORD=" + sentinel], 2,
        "FAILED tests/x.py docker run -e POSTGRES_PASSWORD=" + sentinel + "\n",
        "E fixture_process.FixtureCommandError: docker:OUTPUT_UNREADABLE\n"
        "note: docker:CLEANUP_UNCONFIRMED stderr: Cannot connect " + sentinel + " docker:" + sentinel + "\n")
    message = failure_summary("original_diagnostic_not_observed", nested, injected)
    assert message == ("original_diagnostic_not_observed nested_exit=2 injected=" + ("yes" if injected else "no")
                       + " fixture_codes=docker:CLEANUP_UNCONFIRMED,docker:OUTPUT_UNREADABLE")
    assert sentinel not in message and "POSTGRES_PASSWORD" not in message and "Cannot connect" not in message


def test_failure_summary_reports_absent_codes_explicitly():
    nested = subprocess.CompletedProcess([], 0, "1 passed", "")
    assert failure_summary("nested_failure_not_observed", nested, False) == (
        "nested_failure_not_observed nested_exit=0 injected=no fixture_codes=none")


def test_cleanup_refuses_changed_ownership():
    from unittest.mock import patch

    from fixture_process import FixtureCommandError, OwnedDocker
    fixture = OwnedDocker()
    identity = "a" * 64
    with patch("fixture_process.run", side_effect=[identity, json.dumps({fixture.label_key: "foreign"})]) as tool:
        with pytest.raises(FixtureCommandError, match="OWNERSHIP_UNCONFIRMED"):
            fixture.cleanup()
    assert all("rm" not in call.args for call in tool.call_args_list)
