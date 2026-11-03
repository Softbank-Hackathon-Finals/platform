"""Execute a pinned Gitleaks against two server-prepared text snapshots.

No repository code, hooks, local scanner config, environment credentials or allow
comments are executed/trusted. This tests the supplied revision, not Git history.
The caller owns authentication, registration and immutable snapshot preparation.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .runner import Environment, Observation, RunContext, TestSpec
from .tool_reports import GitleaksMapping, ToolRun, assess_gitleaks

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_FILES = 10_000
MAX_REPORT_BYTES = 10 * 1024 * 1024
VERSION = "8.30.1"
CONFIG = b"[extend]\nuseDefault = true\n"


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class ScanTarget:
    app: str
    environment: Environment
    source: Path
    rendered: Path
    revision: str

    def __post_init__(self):
        if (
            not isinstance(self.environment, Environment)
            or not isinstance(self.app, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.app)
            or not isinstance(self.revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.revision)
            or not all(
                isinstance(path, Path) and path.is_absolute()
                for path in (self.source, self.rendered)
            )
            or self.source == self.rendered
        ):
            raise ValueError("registered distinct snapshots and immutable revision required")


def _snapshot(source: Path, destination: Path) -> str:
    """Reject unsupported inputs instead of silently skipping them.

    open(O_NOFOLLOW) protects the final component. Parent directories must be
    owned by the trusted preparation worker, not writable by an application.
    """
    if source.is_symlink() or not source.is_dir():
        raise ValueError("snapshot directory required")
    destination.mkdir(mode=0o700)
    count = total = directories = 0
    manifest = []

    def walk_error(_error):
        raise ValueError("snapshot traversal incomplete") from None

    for parent, dirs, files in os.walk(source, followlinks=False, onerror=walk_error):
        directories += 1
        if directories > MAX_FILES:
            raise ValueError("snapshot directory budget exceeded")
        for name in dirs:
            child = Path(parent) / name
            if child.is_symlink() or name == ".git":
                raise ValueError("snapshot contains unsupported directory")
        for name in sorted(files):
            child = Path(parent) / name
            relative = child.relative_to(source)
            if len(str(relative)) > 2048 or any(ord(c) < 32 for c in str(relative)):
                raise ValueError("snapshot path unsupported")
            fd = os.open(child, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    raise ValueError("snapshot file unsupported")
                data = stream.read(MAX_FILE_BYTES + 1)
                after = os.fstat(stream.fileno())
                if (
                    len(data) > MAX_FILE_BYTES
                    or len(data) != info.st_size
                    or info.st_mtime_ns != after.st_mtime_ns
                ):
                    raise ValueError("snapshot changed while copying")
            # Binary/archives are outside this qualified path. Never count a
            # scanner skipping one as a clean scan of an arbitrary application.
            data.decode("utf-8", errors="strict")
            if b"\x00" in data:
                raise ValueError("binary snapshot unsupported")
            count += 1
            total += len(data)
            if count > MAX_FILES or total > MAX_TOTAL_BYTES:
                raise ValueError("snapshot exceeds approved scan budget")
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            output.write_bytes(data)
            output.chmod(0o600)
            manifest.append([str(relative), _digest(data)])
    if not count:
        raise ValueError("empty snapshot cannot establish coverage")
    return _digest(_json(sorted(manifest)))


class GitleaksProbe:
    """SEC-01.2, fixed defaults, no app-controlled baseline/config/allowlist.

    Scanner executable SHA must be provided by the deployment's trusted tool
    qualification, not calculated from a user-supplied executable on registration.
    Executable and its parent must be immutable to untrusted users.
    """

    def __init__(
        self,
        target: ScanTarget,
        *,
        executable: Path,
        executable_sha256: str,
        evidence_sink,
        timeout_seconds: int = 60,
        clock=lambda: datetime.now(UTC),
    ):
        if (
            not isinstance(target, ScanTarget)
            or not isinstance(executable, Path)
            or not executable.is_absolute()
            or executable.is_symlink()
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", executable_sha256)
            or type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= 120
            or not callable(evidence_sink)
            or not callable(clock)
        ):
            raise ValueError("qualified scanner configuration required")
        self.target = target
        self.executable = executable
        self.executable_sha256 = executable_sha256
        self.sink = evidence_sink
        self.timeout = timeout_seconds
        self.clock = clock

    def __call__(self, context: RunContext, spec: TestSpec) -> Observation:
        if (
            context.app != self.target.app
            or context.environment != self.target.environment
            or spec != TestSpec("SEC-01.2", "SEC-01", "1", "absent", "not_applicable")
        ):
            raise ValueError("collector scope or method mismatch")
        if (
            self.executable.is_symlink()
            or _digest(self.executable.read_bytes()) != self.executable_sha256
        ):
            raise ValueError("scanner digest mismatch")
        with tempfile.TemporaryDirectory(prefix="bravo-scan-") as scratch:
            root = Path(scratch)
            # Copy executable as well: subsequent path replacement cannot swap
            # the binary between the initial qualification check and execution.
            binary = root / "gitleaks"
            shutil.copyfile(self.executable, binary)
            if _digest(binary.read_bytes()) != self.executable_sha256:
                raise ValueError("scanner changed while copying")
            binary.chmod(0o700)
            env = {"HOME": str(root), "TMPDIR": str(root), "LANG": "C", "LC_ALL": "C"}
            result = subprocess.run(
                [str(binary), "version"],
                env=env,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if result.returncode != 0 or result.stdout.strip() != VERSION.encode():
                raise ValueError("scanner version not qualified")
            config = root / "trusted.toml"
            config.write_bytes(CONFIG)
            ignore = root / "empty.ignore"
            ignore.write_bytes(b"")
            runs = []
            bindings = []
            for name, source in (
                ("source", self.target.source),
                ("rendered", self.target.rendered),
            ):
                snapshot = root / name
                manifest = _snapshot(source, snapshot)
                scope = _digest(
                    _json(
                        {
                            "app": context.app,
                            "environment": context.environment.ref,
                            "revision": self.target.revision,
                            "kind": name,
                            "manifest": manifest,
                            "config": _digest(CONFIG),
                            "scanner": self.executable_sha256,
                        }
                    )
                )
                report = root / (name + ".json")
                with report.open("xb") as output:
                    output.write(b"")
                report.chmod(0o600)
                command = [
                    str(binary),
                    "dir",
                    str(snapshot),
                    "--config",
                    str(config),
                    "--gitleaks-ignore-path",
                    str(ignore),
                    "--ignore-gitleaks-allow",
                    "--redact=100",
                    "--report-format=json",
                    "--report-path",
                    str(report),
                    "--no-banner",
                    "--no-color",
                    "--log-level=error",
                    "--max-decode-depth=5",
                    "--max-archive-depth=0",
                    "--max-target-megabytes=0",
                    "--exit-code=1",
                    "--timeout",
                    str(self.timeout),
                ]
                try:
                    result = subprocess.run(
                        command,
                        env=env,
                        cwd=root,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=self.timeout + 5,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    raise TimeoutError("secret scan timed out") from None
                if report.stat().st_size > MAX_REPORT_BYTES:
                    raise ValueError("scanner report too large")
                output = report.read_bytes()
                runs.append(
                    ToolRun(
                        "gitleaks",
                        VERSION,
                        scope,
                        name,
                        output,
                        result.returncode,
                        result.returncode in (0, 1),
                    )
                )
                bindings.append({"kind": name, "manifest": manifest, "scope": scope})
            assessment = assess_gitleaks(
                *runs, GitleaksMapping(VERSION, runs[0].scope, runs[1].scope)
            )
            # Raw findings can contain file names, match context and secrets even
            # with --redact. Keep only counts and exact output hashes in evidence.
            payload = _json(
                {
                    "format_version": "1",
                    "run_id": context.run_id,
                    "test_id": spec.test_id,
                    "method_version": spec.method_version,
                    "app": context.app,
                    "environment": context.environment.ref,
                    "revision": self.target.revision,
                    "snapshots": bindings,
                    "tool": "gitleaks",
                    "tool_version": VERSION,
                    "tool_sha256": self.executable_sha256,
                    "reports_sha256": assessment.output_sha256,
                    "summary": assessment.summary,
                    "observed": assessment.observed,
                    "unknown_reason": assessment.unknown_reason,
                }
            )
            self.sink(payload)
            return Observation(
                context,
                spec.test_id,
                spec.method_version,
                assessment.observed,
                "not_applicable",
                self.clock(),
                "gitleaks",
                VERSION,
                _digest(payload),
                assessment.unknown_reason,
            )
