"""Fail real nested pytest runs; inspect complete diagnostics and owned Docker residue."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = '''
import os
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
    secrets.token_urlsafe = lambda n: os.environ["FIXTURE_PRIVATE_SENTINEL"]
    def injected(args, *a, **kw):
        __tracebackhide__ = True
        kind = os.environ["FIXTURE_FAULT"]
        create = list(args[:2]) == ["docker", "run"]
        network = list(args[:3]) == ["docker", "network", "create"]
        if create and kind == "exit125":
            return subprocess.CompletedProcess(args, 125, "", "Cannot connect to the Docker daemon")
        result = original(args, *a, **kw)
        if result.returncode == 0 and ((create and kind == "container_reply") or (network and kind == "network_reply")):
            raise UnicodeDecodeError("utf8", b"\\xff", 0, 1, "injected lost create response")
        return result
    subprocess.run = injected
'''


@pytest.mark.parametrize("showlocals", [False, True])
@pytest.mark.parametrize("fixture,fault", [
    ("single", "exit125"), ("pair", "exit125"),
    ("single", "container_reply"), ("pair", "container_reply"),
    ("pair", "network_reply"),
])
def test_failed_pytest_redacts_secrets_and_cleans_lost_creates(tmp_path, showlocals, fixture, fault):
    __tracebackhide__ = True
    plugin = tmp_path / "fault_plugin.py"
    plugin.write_text(PLUGIN)
    owner_file = tmp_path / "owner"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), str(Path(__file__).parent)]),
               FIXTURE_OWNER_FILE=str(owner_file), FIXTURE_FAULT=fault,
               FIXTURE_PRIVATE_SENTINEL="private-fixture-" + os.urandom(16).hex())
    target = "tests/test_t13_capture.py::test_actual_tls_schema_match_then_mismatch_blocks" if fixture == "single" else (
        "tests/test_t13_two_servers.py::test_two_tls_servers_replication_loss_and_sequence_readback")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "fault_plugin", target,
                             *(["--showlocals"] if showlocals else [])],
                            env=env, capture_output=True, text=True, timeout=120)
    output = result.stdout + result.stderr
    if result.returncode != 1:
        pytest.fail("nested_failure_not_observed", pytrace=False)
    if env["FIXTURE_PRIVATE_SENTINEL"] in output:
        pytest.fail("nested_failure_exposed_fixture_credential", pytrace=False)
    code = "DAEMON_UNAVAILABLE" if fault == "exit125" else "OUTPUT_UNREADABLE"
    if code not in output or "CLEANUP_UNCONFIRMED" in output:
        pytest.fail("original_diagnostic_or_cleanup_unconfirmed", pytrace=False)
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
