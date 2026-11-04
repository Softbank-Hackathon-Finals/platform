"""Bounded diagnostics for local-only test infrastructure, never raw command errors."""
import subprocess


class FixtureCommandError(RuntimeError):
    """A fixed code only: no argv, environment, stderr, or connection parameters."""


def failure_code(stderr):
    text = (stderr or "").lower()
    for needles, code in (
        (("cannot connect to the docker daemon", "error during connect"), "DAEMON_UNAVAILABLE"),
        (("no space left on device",), "STORAGE_EXHAUSTED"),
        (("toomanyrequests", "too many requests"), "REGISTRY_RATE_LIMITED"),
        (("pull access denied", "unauthorized", "denied: requested access"), "IMAGE_ACCESS_DENIED"),
        (("port is already allocated", "address already in use"), "PORT_CONFLICT"),
        (("no matching manifest", "manifest unknown"), "IMAGE_UNAVAILABLE"),
    ):
        if any(needle in text for needle in needles):
            return code
    return "UNCLASSIFIED_FAILURE"


def run(*args, env=None):
    __tracebackhide__ = True  # pytest must not render command/environment locals.
    tool = args[0] if args and args[0] in {"docker", "openssl"} else "fixture_tool"
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True,
                                check=False, timeout=90)
    except subprocess.TimeoutExpired:
        raise FixtureCommandError(f"{tool}:TIMEOUT") from None
    except UnicodeError:
        raise FixtureCommandError(f"{tool}:OUTPUT_UNREADABLE") from None
    except OSError:
        raise FixtureCommandError(f"{tool}:PROCESS_UNAVAILABLE") from None
    if result.returncode:
        code = failure_code(result.stderr) if tool == "docker" else "UNCLASSIFIED_FAILURE"
        raise FixtureCommandError(f"{tool}:{code}:exit={result.returncode}")
    return result.stdout.strip()
