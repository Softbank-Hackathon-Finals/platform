"""Bounded diagnostics for local-only test infrastructure, never raw command errors."""
import json
import re
import secrets
import subprocess


class FixtureCommandError(RuntimeError):
    """A fixed code only: no argv, environment, stderr, or connection parameters."""


# Every code a FixtureCommandError can carry. Failure summaries report only these.
TOOLS = ("docker", "openssl", "fixture_tool")
CODES = (
    "DAEMON_UNAVAILABLE", "STORAGE_EXHAUSTED", "REGISTRY_RATE_LIMITED", "IMAGE_ACCESS_DENIED",
    "PORT_CONFLICT", "IMAGE_UNAVAILABLE", "UNCLASSIFIED_FAILURE", "TIMEOUT", "OUTPUT_UNREADABLE",
    "PROCESS_UNAVAILABLE", "CLEANUP_ID_UNCONFIRMED", "CLEANUP_OWNERSHIP_UNCONFIRMED",
    "CLEANUP_REMAINS", "CLEANUP_UNCONFIRMED",
)
_REPORTABLE = re.compile(
    r"(?<![A-Za-z0-9_])(?:" + "|".join(TOOLS) + r"):(?:" + "|".join(CODES)
    + r")(?::exit=\d{1,3})?(?![A-Za-z0-9_])")


def observed_codes(text):
    """The fixed fixture codes found in text, sorted and unique. Nothing else is returned."""
    return sorted({match.group(0) for match in _REPORTABLE.finditer(text or "")})


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


class OwnedDocker:
    """One fixture's labels and exact IDs, including lost create responses."""

    label_key = "bravo.test-fixture"

    def __init__(self):
        self.owner = secrets.token_hex(16)
        self.label = f"{self.label_key}={self.owner}"

    def __enter__(self):
        return self

    def network(self, name):
        return run("docker", "network", "create", "--label", self.label, name)

    def container(self, *args, env=None):
        __tracebackhide__ = True
        return run("docker", "run", "--label", self.label, *args, env=env)

    def _ids(self, kind):
        listing = run("docker", kind, "ls", "--quiet", "--no-trunc",
                      *( ["--all"] if kind == "container" else []),
                      "--filter", "label=" + self.label)
        ids = listing.splitlines()
        if any(not re.fullmatch(r"[a-f0-9]{64}", item) for item in ids):
            raise FixtureCommandError("docker:CLEANUP_ID_UNCONFIRMED")
        return ids

    def cleanup(self):
        # Never retry creates. Discovery includes resources whose create reply was lost.
        for kind in ("container", "network"):
            for identity in self._ids(kind):
                template = "{{json .Config.Labels}}" if kind == "container" else "{{json .Labels}}"
                labels = json.loads(run("docker", kind, "inspect", "--format", template, identity))
                if not isinstance(labels, dict) or labels.get(self.label_key) != self.owner:
                    raise FixtureCommandError("docker:CLEANUP_OWNERSHIP_UNCONFIRMED")
                run("docker", kind, "rm", *( ["-fv"] if kind == "container" else []), identity)
            if self._ids(kind):
                raise FixtureCommandError("docker:CLEANUP_REMAINS")

    def __exit__(self, exc_type, exc, tb):
        __tracebackhide__ = True
        try:
            self.cleanup()
        except Exception:
            if exc is not None:
                exc.add_note("docker:CLEANUP_UNCONFIRMED")
            else:
                raise FixtureCommandError("docker:CLEANUP_UNCONFIRMED") from None
        return False
