import subprocess
import traceback
from unittest.mock import patch

import pytest
from fixture_process import CODES, FixtureCommandError, failure_code, observed_codes, run

SENTINEL = "PRIVATE_FIXTURE_SENTINEL"


@pytest.mark.parametrize("stderr,code", [
    ("Cannot connect to the Docker daemon", "DAEMON_UNAVAILABLE"),
    ("no space left on device", "STORAGE_EXHAUSTED"),
    ("toomanyrequests: quota", "REGISTRY_RATE_LIMITED"),
    ("pull access denied", "IMAGE_ACCESS_DENIED"),
    ("port is already allocated", "PORT_CONFLICT"),
    ("manifest unknown", "IMAGE_UNAVAILABLE"),
    ("unknown failure", "UNCLASSIFIED_FAILURE"),
])
def test_failure_reports_only_fixed_category_without_retry(stderr, code):
    response = subprocess.CompletedProcess(("docker", "run"), 125,
                                           SENTINEL, stderr + SENTINEL)
    with patch("fixture_process.subprocess.run", return_value=response) as call:
        with pytest.raises(FixtureCommandError) as caught:
            run("docker", "run", "-e", "POSTGRES_PASSWORD", env={"POSTGRES_PASSWORD": SENTINEL})
    assert str(caught.value) == f"docker:{code}:exit=125"
    assert observed_codes(str(caught.value)) == [str(caught.value)]  # every category stays reportable
    assert SENTINEL not in "".join(traceback.format_exception(caught.value))
    assert call.call_count == 1
    assert SENTINEL not in repr(call.call_args.args)
    assert call.call_args.kwargs["env"]["POSTGRES_PASSWORD"] == SENTINEL
    assert call.call_args.kwargs["check"] is False


@pytest.mark.parametrize("error,code", [
    (subprocess.TimeoutExpired([SENTINEL], 90, output=SENTINEL, stderr=SENTINEL), "TIMEOUT"),
    (OSError(SENTINEL), "PROCESS_UNAVAILABLE"),
    (UnicodeDecodeError("utf8", b"PRIVATE_FIXTURE_SENTINEL\xff", 0, 1, "bad"), "OUTPUT_UNREADABLE"),
])
def test_timeout_and_launch_error_hide_original_exception(error, code):
    with patch("fixture_process.subprocess.run", side_effect=error) as call:
        with pytest.raises(FixtureCommandError) as caught:
            run("docker", "info")
    assert str(caught.value) == f"docker:{code}"
    assert observed_codes(str(caught.value)) == [str(caught.value)]
    assert SENTINEL not in "".join(traceback.format_exception(caught.value))
    assert call.call_count == 1


def test_observed_codes_return_only_the_fixed_vocabulary():
    text = (f"argv: docker run -e POSTGRES_PASSWORD={SENTINEL}\n"
            "E   fixture_process.FixtureCommandError: docker:DAEMON_UNAVAILABLE:exit=125\n"
            "note: docker:CLEANUP_UNCONFIRMED openssl:TIMEOUT docker:DAEMON_UNAVAILABLE:exit=125\n"
            f"docker:{SENTINEL} docker:EVIL_CODE xdocker:TIMEOUT docker:TIMEOUTX docker:PORT_CONFLICT:exit=1234\n"
            f"stderr: Cannot connect to the Docker daemon {SENTINEL}")
    codes = observed_codes(text)
    assert codes == ["docker:CLEANUP_UNCONFIRMED", "docker:DAEMON_UNAVAILABLE:exit=125",
                     "docker:PORT_CONFLICT", "openssl:TIMEOUT"]
    assert SENTINEL not in repr(codes)
    assert observed_codes("") == [] and observed_codes(None) == []


def test_every_stderr_category_is_in_the_reportable_codes():
    for stderr in ("Cannot connect to the Docker daemon", "error during connect", "no space left on device",
                   "toomanyrequests", "pull access denied", "port is already allocated",
                   "manifest unknown", "anything else"):
        assert failure_code(stderr) in CODES


def test_success_retains_result_for_fixture_setup():
    with patch("fixture_process.subprocess.run", return_value=subprocess.CompletedProcess([],0,"id\n","")):
        assert run("docker", "run") == "id"
