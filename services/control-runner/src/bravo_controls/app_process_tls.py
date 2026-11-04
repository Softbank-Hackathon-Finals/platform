"""T14 read-only TLS observations inside an existing registered app boundary.

This is not the isolated psql runner and does not claim short-lived DB credentials.
The trusted host binds live Kubernetes/credential readback and durable evidence.
"""

import hashlib
import json
import re
import socket
import ssl
import struct
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .postgres_tls import MAX_BINARY, PoolerTarget, _execute
from .runner import Observation, RunContext, TestSpec

SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
CLIENT = Path(__file__).with_name("app_process_tls_client.py")


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def encoded(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class AppProcessRegistration:
    target: PoolerTarget
    deployment_uid: str
    image_digest: str
    credential_reference_sha256: str
    ca_reference_sha256: str
    python: Path
    python_sha256: str
    client_sha256: str
    psycopg_version: str
    libpq_version: int

    def __post_init__(self):
        if (not isinstance(self.target, PoolerTarget)
                or not isinstance(self.deployment_uid, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", self.deployment_uid)
                or any(not isinstance(x, str) or not SHA.fullmatch(x) for x in (
                    self.image_digest, self.credential_reference_sha256, self.ca_reference_sha256,
                    self.python_sha256, self.client_sha256))
                or not isinstance(self.python, Path) or not self.python.is_absolute()
                or not isinstance(self.psycopg_version, str)
                or not re.fullmatch(r"3\.[0-9]+\.[0-9]+", self.psycopg_version)
                or type(self.libpq_version) is not int or not 180000 <= self.libpq_version < 190000):
            raise ValueError("qualified app runtime and server registration required")

    def binding(self):
        target = self.target
        return {
            "app": target.app, "environment": vars(target.environment),
            "namespace": target.namespace, "app_pod_uid": target.runner_pod_uid,
            "deployment_uid": self.deployment_uid, "image_digest": self.image_digest,
            "pooler_uid": target.pooler_uid, "registration_sha256": target.registration_sha256,
            "hostname": target.hostname, "address": target.address, "port": target.port,
            "database": target.database, "username": target.username,
            "ca_sha256": digest(target.ca_pem), "wrong_ca_sha256": digest(target.wrong_ca_pem),
            "wrong_hostname": target.wrong_hostname,
            "ca_reference_sha256": self.ca_reference_sha256,
            "credential_reference_sha256": self.credential_reference_sha256,
            "python_sha256": self.python_sha256, "client_sha256": self.client_sha256,
            "psycopg_version": self.psycopg_version, "libpq_version": self.libpq_version,
        }


def _recv(sock, length, deadline):
    if not 0 <= length <= 8192:
        raise ValueError("bounded protocol response required")
    value = b""
    while len(value) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        sock.settimeout(remaining)
        part = sock.recv(length - len(value))
        if not part:
            raise ValueError("incomplete protocol response")
        value += part
    return value


def _wire(target, mode):
    """No password or application query is sent on negative connections."""
    deadline = time.monotonic() + 5
    with socket.create_connection((target.address, target.port), timeout=5) as raw:
        if mode in ("wrong_ca", "wrong_host"):
            raw.sendall(struct.pack("!II", 8, 80877103))
            if _recv(raw, 1, deadline) != b"S":
                return {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_verify_locations(cadata=(target.wrong_ca_pem if mode == "wrong_ca"
                                              else target.ca_pem).decode("ascii"))
            raw.settimeout(max(.001, deadline - time.monotonic()))
            try:
                with ctx.wrap_socket(raw, server_hostname=(target.wrong_hostname
                                     if mode == "wrong_host" else target.hostname)):
                    return {"outcome": "allow", "reason": None}
            except ssl.SSLCertVerificationError as exc:
                # Qualified OpenSSL trust-chain errors vs hostname mismatch.
                allowed = {18, 19, 20, 21} if mode == "wrong_ca" else {62}
                if exc.verify_code in allowed:
                    return {"outcome": "deny", "reason": None}
                return {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}
        payload = (struct.pack("!I", 196608) + b"user\0" + target.username.encode()
                   + b"\0database\0" + target.database.encode() + b"\0\0")
        raw.sendall(struct.pack("!I", len(payload) + 4) + payload)
        header = _recv(raw, 5, deadline)
        body = _recv(raw, struct.unpack("!I", header[1:])[0] - 4, deadline)
        if header[:1] == b"R":
            method = int.from_bytes(body[:4], "big")
            # Only well-formed initial authentication messages prove plaintext
            # startup was accepted. Continuation/unknown/truncated frames do not.
            sizes = {0: 4, 2: 4, 3: 4, 5: 8, 7: 4, 9: 4}
            if len(body) == sizes.get(method) or (
                method == 10 and re.fullmatch(rb"(?:[A-Z0-9_-]{1,20}\x00)+\x00", body[4:])
            ):
                return {"outcome": "allow", "reason": None}
            return {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}
        if header[:1] != b"E" or not body.endswith(b"\0\0"):
            return {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}
        fields = {}
        for field in body[:-2].split(b"\0"):
            if not field or field[:1] in fields:
                return {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}
            fields[field[:1]] = field[1:]
        if not fields.get(b"S") or not fields.get(b"M") or not re.fullmatch(rb"[A-Z0-9]{5}", fields.get(b"C", b"")):
            return {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}
        if fields.get(b"C") == b"08P01" and fields.get(b"M") in (b"SSL required", b"TLS required"):
            return {"outcome": "deny", "reason": None}
        return {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}


class AppProcessTLSProbe:
    def __init__(self, registration, *, credential_provider, binding_reader, evidence_sink,
                 clock=lambda: datetime.now(UTC)):
        if (not isinstance(registration, AppProcessRegistration)
                or not all(callable(x) for x in (credential_provider, binding_reader, evidence_sink, clock))):
            raise ValueError("trusted registration, credential/readback/retention providers required")
        self.registration = registration
        self.credentials, self.readback, self.sink, self.clock = credential_provider, binding_reader, evidence_sink, clock

    def __call__(self, context: RunContext, spec: TestSpec):
        reg, target = self.registration, self.registration.target
        expected = {
            "ENC-01.1": TestSpec("ENC-01.1", "ENC-01", "1", "success", "not_applicable"),
            "ENC-01.2": TestSpec("ENC-01.2", "ENC-01", "1", "deny", "required"),
            "ENC-01.3": TestSpec("ENC-01.3", "ENC-01", "1", "deny", "required"),
        }
        if (not isinstance(context, RunContext) or not isinstance(spec, TestSpec)
                or context.app != target.app or context.environment != target.environment
                or expected.get(spec.test_id) != spec):
            raise ValueError("registered scope/method mismatch")
        binding = digest(encoded(reg.binding()))
        if self.readback(context) != binding:
            raise ValueError("live app/credential binding mismatch")
        for path, expected_hash in ((reg.python.resolve(strict=True), reg.python_sha256), (CLIENT, reg.client_sha256)):
            with path.open("rb") as stream:
                content = stream.read(MAX_BINARY + 1)
            if len(content) > MAX_BINARY or digest(content) != expected_hash:
                raise ValueError("unqualified executable")
        password = self.credentials(context)
        if not isinstance(password, str) or not 1 <= len(password) <= 4096 or "\0" in password:
            raise ValueError("registered app credential required")
        checks = []
        with tempfile.TemporaryDirectory(prefix="bravo-app-tls-") as scratch:
            root = Path(scratch)
            (root / "ca.pem").write_bytes(target.ca_pem)  # Public CA only.
            config = {name: getattr(target, name) for name in ("hostname", "address", "port", "database", "username")}
            config.update(ca_file=str(root / "ca.pem"), psycopg_version=reg.psycopg_version, libpq_version=reg.libpq_version)

            def check(mode):
                try:
                    if mode == "positive":
                        code, output, error = _execute(
                            [str(reg.python), "-I", str(CLIENT)],
                            env={"HOME": str(root), "LANG": "C", "LC_ALL": "C",
                                 "PGPASSWORD": password, "BRAVO_TLS_CONFIG": encoded(config).decode()},
                            cwd=root, timeout=10,
                        )
                        result = json.loads(output) if not code and not error else {}
                        if result not in ({"outcome": "allow", "reason": None},
                                          {"outcome": None, "reason": "TOOL_ERROR"},
                                          {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}):
                            result = {"outcome": None, "reason": "UNEXPECTED_RESPONSE"}
                    else:
                        result = _wire(target, mode)
                except TimeoutError:
                    result = {"outcome": None, "reason": "TIMEOUT"}
                except (OSError, ValueError):
                    result = {"outcome": None, "reason": "TOOL_ERROR"}
                checks.append({"mode": mode, **result})
                return result

            before = check("positive")
            observed, reason = None, None
            positive = "not_applicable" if spec.test_id == "ENC-01.1" else "fail"
            if spec.test_id == "ENC-01.1":
                observed = "success" if before["outcome"] == "allow" else None
                reason = before["reason"]
            elif before["outcome"] != "allow":
                reason = "POSITIVE_CONTROL_FAILED"
            else:
                negatives = [check(mode) for mode in (("wrong_ca", "wrong_host")
                             if spec.test_id == "ENC-01.2" else ("plain",))]
                after = check("positive")
                if after["outcome"] != "allow":
                    reason = "POSITIVE_CONTROL_FAILED"
                else:
                    positive = "pass"
                    if any(x["outcome"] == "allow" for x in negatives):
                        observed = "allow"
                    elif all(x["outcome"] == "deny" for x in negatives):
                        observed = "deny"
                    else:
                        reason = next(x["reason"] for x in negatives if x["reason"])
            password = ""
        if self.readback(context) != binding:
            raise ValueError("live binding changed during probe")
        collected = self.clock()
        if not isinstance(collected, datetime) or collected.tzinfo is None:
            raise ValueError("aware collection time required")
        payload = encoded({
            "format_version": "1", "collector": "bravo-app-process-tls", "collector_version": "1",
            "run_id": context.run_id, "app": context.app, "environment": vars(context.environment),
            "test_id": spec.test_id, "method_version": spec.method_version,
            "binding_sha256": binding, "python_sha256": reg.python_sha256,
            "client_sha256": reg.client_sha256, "psycopg_version": reg.psycopg_version,
            "libpq_version": reg.libpq_version, "collected_at": collected.astimezone(UTC).isoformat(),
            "checks": checks, "plaintext_scope": "startup_without_password",
        })
        output_hash = digest(payload)
        if self.sink(payload) != {"run_id": context.run_id, "output_sha256": output_hash}:
            raise ValueError("durable evidence acknowledgement mismatch")
        return Observation(context, spec.test_id, spec.method_version, observed, positive,
                           collected.astimezone(UTC), "bravo-app-process-tls", "1", output_hash, reason)
