"""Bounded psql probes for the client→pooler ENC-01.1/2/3 controls.

Run inside the registered app namespace, using an immutable runner image. The
caller authenticates the registration and binds the actual Pod UID, pooler UID,
address, CA and app credential. Labels supplied by an uploader are not proof.
This module never establishes the pooler→DB or replication TLS controls.
"""

import hashlib
import ipaddress
import json
import os
import re
import selectors
import ssl
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .runner import Environment, Observation, RunContext, TestSpec

MAX_OUTPUT = 65536
MAX_BINARY = 32 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class PoolerTarget:
    app: str
    environment: Environment
    namespace: str
    runner_pod_uid: str
    pooler_uid: str
    registration_sha256: str
    hostname: str
    address: str
    port: int
    database: str
    username: str
    ca_pem: bytes
    wrong_ca_pem: bytes
    wrong_hostname: str = "wrong-pooler.invalid"

    def __post_init__(self):
        if (
            not isinstance(self.environment, Environment)
            or not all(
                isinstance(v, str) and _NAME.fullmatch(v)
                for v in (
                    self.app,
                    self.namespace,
                    self.runner_pod_uid,
                    self.pooler_uid,
                    self.hostname,
                    self.wrong_hostname,
                    self.database,
                    self.username,
                )
            )
            or not isinstance(self.registration_sha256, str)
            or not _SHA.fullmatch(self.registration_sha256)
            or type(self.port) is not int
            or not 1 <= self.port <= 65535
            or self.hostname == self.wrong_hostname
        ):
            raise ValueError("server-registered pooler and namespace binding required")
        ipaddress.ip_address(self.address)  # One literal address; no DNS/failover list.
        for value in (self.ca_pem, self.wrong_ca_pem):
            if not isinstance(value, bytes) or not 1 <= len(value) <= MAX_OUTPUT:
                raise ValueError("bounded CA bundle required")
            if b"PRIVATE KEY" in value:
                raise ValueError("public certificates only")
            try:
                ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(
                    cadata=value.decode("ascii")
                )
            except (ValueError, UnicodeError, ssl.SSLError):
                raise ValueError("valid PEM CA bundle required") from None
        if self.ca_pem == self.wrong_ca_pem:
            raise ValueError("independent negative CA required")


def _execute(command, *, env, cwd, timeout=10):
    """No shell, stdin, inherited environment or unbounded pipe buffering."""
    with subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                for name in buffers:
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("database probe deadline exceeded")
                    for key, _ in selector.select(min(remaining, 0.25)):
                        data = os.read(key.fd, 4096)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        buffers[key.data].extend(data)
                        if sum(map(len, buffers.values())) > MAX_OUTPUT:
                            raise ValueError("database probe output budget exceeded")
            code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            return code, bytes(buffers["stdout"]), bytes(buffers["stderr"])
        except subprocess.TimeoutExpired:
            raise TimeoutError("database probe deadline exceeded") from None
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def _result(mode, code, output, error):
    # Raw stdout/stderr can contain addresses, database/user names or hostile
    # server messages. They are neither logged nor persisted, even as a hash.
    if code == 0 and not error:
        lines = output.decode("utf-8", errors="replace").splitlines()
        if lines.count("1") != 1:
            return {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}
        if mode == "plain":
            return {"outcome": "allow", "reason": None}
        fields = [line.split("|", 1) for line in lines if line.startswith("SSL ")]
        names = [field[0] for field in fields]
        tls = dict(field for field in fields if len(field) == 2)
        if (
            len(names) == len(set(names)) == len(tls)
            and tls.get("SSL Connection") == "true"
            and tls.get("SSL Protocol") in {"TLSv1.2", "TLSv1.3"}
            and tls.get("SSL Compression") == "false"
        ):
            return {"outcome": "allow", "reason": None}
        return {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}
    if code != 2 or output:
        return {"outcome": None, "reason": "TOOL_ERROR"}
    # Only qualified C-locale libpq/PgBouncer errors. A timeout, EOF, wrong
    # password, missing certificate file or arbitrary FATAL is not policy proof.
    text = error.decode("utf-8", errors="replace")
    envelope = re.fullmatch(
        r'psql: error: connection to server at "[^"\r\n]+"(?: \([0-9a-fA-F:.]+\))?, port [0-9]+ failed: ([^\r\n]+)\r?\n',
        text,
    )
    detail = envelope[1] if envelope else ""
    denied = (
        mode == "wrong_ca"
        and detail == "SSL error: certificate verify failed"
        or mode == "wrong_host"
        and re.fullmatch(
            r'server certificate (?:for "[^"\r\n]+"(?: \(and [0-9]+ other names\))?|contains [^\r\n]+) does not match host name "[^"\r\n]+"',
            detail,
        )
        or mode == "plain"
        and (
            detail in {"FATAL:  SSL required", "FATAL:  TLS required"}
            or re.fullmatch(
                r"FATAL:  (?:no pg_hba.conf entry|pg_hba.conf rejects connection) for host [^\r\n]+, no encryption",
                detail,
            )
        )
    )
    if denied:
        return {"outcome": "deny", "reason": None}
    return {"outcome": None, "reason": "TOOL_ERROR"}


class PoolerTLSProbe:
    def __init__(
        self,
        target: PoolerTarget,
        *,
        executable: Path,
        executable_sha256: str,
        psql_version: str,
        credential_provider,
        evidence_sink,
        clock=lambda: datetime.now(UTC),
    ):
        if (
            not isinstance(target, PoolerTarget)
            or not isinstance(executable, Path)
            or not executable.is_absolute()
            or not isinstance(executable_sha256, str)
            or not _SHA.fullmatch(executable_sha256)
            or not isinstance(psql_version, str)
            or not re.fullmatch(
                r"18\.[0-9]{1,2}(?: \(Debian [A-Za-z0-9.+~_-]{1,48}\))?", psql_version
            )
            or not all(callable(x) for x in (credential_provider, evidence_sink, clock))
        ):
            raise ValueError("qualified psql, credential provider and retained evidence required")
        self.target = target
        self.executable = executable
        self.executable_sha256 = executable_sha256
        self.version = psql_version
        self.credentials = credential_provider
        self.sink = evidence_sink
        self.clock = clock

    def __call__(self, context: RunContext, spec: TestSpec) -> Observation:
        expected = {
            "ENC-01.1": TestSpec("ENC-01.1", "ENC-01", "1", "success", "not_applicable"),
            "ENC-01.2": TestSpec("ENC-01.2", "ENC-01", "1", "deny", "required"),
            "ENC-01.3": TestSpec("ENC-01.3", "ENC-01", "1", "deny", "required"),
        }
        if (
            not isinstance(context, RunContext)
            or not isinstance(spec, TestSpec)
            or not _NAME.fullmatch(context.run_id)
            or context.app != self.target.app
            or context.environment != self.target.environment
            or expected.get(spec.test_id) != spec
        ):
            raise ValueError("collector scope or method mismatch")
        with tempfile.TemporaryDirectory(prefix="bravo-pg-tls-") as scratch:
            root = Path(scratch)
            fd = os.open(self.executable, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BINARY:
                    raise ValueError("qualified psql binary required")
                binary = stream.read(MAX_BINARY + 1)
            if len(binary) > MAX_BINARY or _digest(binary) != self.executable_sha256:
                raise ValueError("psql digest mismatch")
            executable = root / "psql"
            executable.write_bytes(binary)
            executable.chmod(0o700)
            base_env = {"HOME": str(root), "TMPDIR": str(root), "LANG": "C", "LC_ALL": "C"}
            code, output, error = _execute([str(executable), "--version"], env=base_env, cwd=root)
            if code or error or output.strip() != f"psql (PostgreSQL) {self.version}".encode():
                raise ValueError("psql version not qualified")
            for filename, data in (
                ("ca.pem", self.target.ca_pem),
                ("wrong-ca.pem", self.target.wrong_ca_pem),
            ):
                (root / filename).write_bytes(data)
            password = self.credentials(context)
            if not isinstance(password, str) or not 1 <= len(password) <= 4096 or "\0" in password:
                raise ValueError("registered app credential required")
            checks = []

            def check(mode):
                env = base_env | {
                    "PGHOST": self.target.wrong_hostname
                    if mode == "wrong_host"
                    else self.target.hostname,
                    "PGHOSTADDR": self.target.address,
                    "PGPORT": str(self.target.port),
                    "PGDATABASE": self.target.database,
                    "PGUSER": self.target.username,
                    "PGPASSWORD": password,
                    "PGCONNECT_TIMEOUT": "5",
                    "PGSSLMODE": "disable" if mode == "plain" else "verify-full",
                    "PGSSLROOTCERT": str(
                        root / ("wrong-ca.pem" if mode == "wrong_ca" else "ca.pem")
                    ),
                    "PGSSLMINPROTOCOLVERSION": "TLSv1.2",
                    "PGGSSENCMODE": "disable",
                    "PGREQUIREAUTH": "scram-sha-256",
                    "PGAPPNAME": "bravo-tls-probe",
                    "PGCLIENTENCODING": "UTF8",
                }
                try:
                    result = _result(
                        mode,
                        *_execute(
                            [
                                str(executable),
                                "-X",
                                "-w",
                                "-A",
                                "-t",
                                "-v",
                                "ON_ERROR_STOP=1",
                                "-c",
                                "\\conninfo",
                                "-c",
                                "SELECT 1;",
                            ],
                            env=env,
                            cwd=root,
                        ),
                    )
                except TimeoutError:
                    result = {"outcome": None, "reason": "TIMEOUT"}
                except (OSError, ValueError):
                    result = {"outcome": None, "reason": "TOOL_ERROR"}
                checks.append({"mode": mode, **result})
                return result

            positive = check("positive")
            observed = reason = None
            positive_control = "not_applicable" if spec.test_id == "ENC-01.1" else "fail"
            if spec.test_id == "ENC-01.1":
                observed = "success" if positive["outcome"] == "allow" else None
                reason = positive["reason"]
            elif positive["outcome"] != "allow":
                reason = "POSITIVE_CONTROL_FAILED"
            else:
                modes = ("wrong_ca", "wrong_host") if spec.test_id == "ENC-01.2" else ("plain",)
                negatives = [check(mode) for mode in modes]
                after = check("positive")
                if after["outcome"] != "allow":
                    reason = "POSITIVE_CONTROL_FAILED"
                else:
                    positive_control = "pass"
                    if any(x["outcome"] == "allow" for x in negatives):
                        observed = "allow"
                    elif all(x["outcome"] == "deny" for x in negatives):
                        observed = "deny"
                    else:
                        reason = next(x["reason"] for x in negatives if x["reason"])
            password = ""
        collected = self.clock()
        if (
            not isinstance(collected, datetime)
            or collected.tzinfo is None
            or collected.utcoffset() is None
        ):
            raise ValueError("aware collection time required")
        binding = {
            name: getattr(self.target, name)
            for name in (
                "namespace",
                "runner_pod_uid",
                "pooler_uid",
                "registration_sha256",
                "hostname",
                "address",
                "port",
                "database",
                "username",
                "wrong_hostname",
            )
        } | {
            "ca_sha256": _digest(self.target.ca_pem),
            "wrong_ca_sha256": _digest(self.target.wrong_ca_pem),
        }
        payload = _json(
            {
                "format_version": "1",
                "run_id": context.run_id,
                "app": context.app,
                "environment": {
                    "id": context.environment.id,
                    "role": context.environment.role,
                    "ref": context.environment.ref,
                },
                "test_id": spec.test_id,
                "method_version": "1",
                "binding_sha256": _digest(_json(binding)),
                "psql_sha256": self.executable_sha256,
                "psql_version": self.version,
                "collected_at": collected.astimezone(UTC).isoformat(),
                "checks": checks,
            }
        )
        self.sink(payload)  # A failed durable write never yields a successful observation.
        return Observation(
            context,
            spec.test_id,
            "1",
            observed,
            positive_control,
            collected.astimezone(UTC),
            "bravo-controls-psql",
            "1",
            _digest(payload),
            reason,
        )
