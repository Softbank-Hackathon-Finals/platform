"""NET-01.1: a fixed Nmap TCP scan, bound to a trusted external inventory.

Not an Internet-scanning API. The host owns authorization, complete node/LB
inventory and external worker placement. IPv4/TCP only; UDP is not qualified.
"""

import hashlib
import ipaddress
import json
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .runner import Environment, Observation, RunContext, TestSpec, _identifier, _utc

MAX_XML = 32 * 1024 * 1024
SPEC = TestSpec("NET-01.1", "NET-01", "1", "absent", "required")


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


@dataclass(frozen=True)
class ScanHost:
    uid: str
    ip: str
    allowed_ports: frozenset[int]

    def __post_init__(self):
        _identifier(self.uid)
        address = ipaddress.IPv4Address(self.ip)
        if (
            str(address) != self.ip
            or address.is_unspecified
            or address.is_multicast
            or self.ip == "255.255.255.255"
            or not isinstance(self.allowed_ports, frozenset)
            or any(type(p) is not int or not 1 <= p <= 65535 for p in self.allowed_ports)
        ):
            raise ValueError("registered IPv4 and TCP allowlist required")


@dataclass(frozen=True)
class PublicScanTarget:
    app: str
    environment: Environment
    external_agent: str
    hosts: tuple[ScanHost, ...]
    ingress_ref: str
    inventory_revision: str

    def __post_init__(self):
        for value in (self.app, self.external_agent, self.ingress_ref, self.inventory_revision):
            _identifier(value)
        if (
            not isinstance(self.environment, Environment)
            or not isinstance(self.hosts, tuple)
            or not 1 <= len(self.hosts) <= 8
            or not all(isinstance(h, ScanHost) for h in self.hosts)
            or len({h.uid for h in self.hosts}) != len(self.hosts)
            or len({h.ip for h in self.hosts}) != len(self.hosts)
        ):
            raise ValueError("complete distinct registered node/LB inventory required")

    @property
    def binding(self):
        return digest(
            canonical(
                {
                    "app": self.app,
                    "environment": asdict(self.environment),
                    "external_agent": self.external_agent,
                    "ingress": self.ingress_ref,
                    "revision": self.inventory_revision,
                    "hosts": [
                        {"uid": h.uid, "ip": h.ip, "allowed": sorted(h.allowed_ports)}
                        for h in self.hosts
                    ],
                    "protocol": "tcp",
                    "ports": "1-65535",
                }
            )
        )


@dataclass(frozen=True)
class IngressReceipt:
    context: RunContext
    binding: str
    nonce: str
    agent: str
    ingress_ref: str
    responded: bool
    observed_at: datetime


def parse_scan(raw: bytes, host: ScanHost, version: str) -> tuple[tuple[int, ...], bool]:
    """Validate full TCP coverage, then return unexpected opens and ambiguity.

    Nmap's ordinary empty DOCTYPE is accepted; entities/other declarations are
    rejected before ElementTree. Never deserialize arbitrary uploaded reports.
    """
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_XML:
        raise ValueError("scan output size invalid")
    # Reject UTF-16 etc. before declaration checks; valid Nmap output is UTF-8.
    text = raw.decode("utf-8", errors="strict")
    if "\x00" in text:
        raise ValueError("unsupported XML encoding")
    text = text.replace("<!DOCTYPE nmaprun>", "")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise ValueError("XML declaration unsupported")
    root = ET.fromstring(text)
    if root.tag != "nmaprun" or root.get("scanner") != "nmap" or root.get("version") != version:
        raise ValueError("unqualified report")
    scans = root.findall("scaninfo")
    if len(scans) != 1 or any(
        scans[0].get(k) != v
        for k, v in {
            "type": "connect",
            "protocol": "tcp",
            "numservices": "65535",
            "services": "1-65535",
        }.items()
    ):
        raise ValueError("incomplete scan coverage")
    stats = root.findall("runstats")
    if len(stats) != 1:
        raise ValueError("missing completion")
    finish, counts = stats[0].findall("finished"), stats[0].findall("hosts")
    if (
        len(finish) != 1
        or finish[0].get("exit") != "success"
        or len(counts) != 1
        or any(counts[0].get(k) != v for k, v in {"up": "1", "down": "0", "total": "1"}.items())
    ):
        raise ValueError("scan not completed")
    hosts = root.findall("host")
    if len(hosts) != 1 or hosts[0].get("timedout", "false") != "false":
        raise ValueError("host missing or timed out")
    item = hosts[0]
    statuses = item.findall("status")
    addresses = [a for a in item.findall("address") if a.get("addrtype") == "ipv4"]
    if (
        len(statuses) != 1
        or statuses[0].get("state") != "up"
        or len(addresses) != 1
        or addresses[0].get("addr") != host.ip
    ):
        raise ValueError("host identity mismatch")
    groups = item.findall("ports")
    if len(groups) != 1:
        raise ValueError("missing port results")
    seen, unexpected, ambiguous, summarized = set(), [], False, 0
    summary_states = set()
    for port in groups[0]:
        if port.tag == "extraports":
            count = int(port.get("count", "-1"))
            # Open summaries cannot identify which allowlisted port was open.
            if not 1 <= count <= 65535 or port.get("state") not in {"closed", "filtered"}:
                raise ValueError("unsupported port summary")
            if port.get("state") in summary_states:
                raise ValueError("duplicate summary state")
            summary_states.add(port.get("state"))
            summarized += count
            ambiguous |= port.get("state") == "filtered"
        elif port.tag == "port":
            number = int(port.get("portid", "-1"))
            states = port.findall("state")
            if (
                port.get("protocol") != "tcp"
                or not 1 <= number <= 65535
                or number in seen
                or len(states) != 1
            ):
                raise ValueError("invalid or duplicate TCP port")
            seen.add(number)
            state = states[0].get("state")
            if state not in {
                "open",
                "closed",
                "filtered",
                "open|filtered",
                "closed|filtered",
                "unfiltered",
            }:
                raise ValueError("unrecognized port state")
            if state == "open" and number not in host.allowed_ports:
                unexpected.append(number)
            ambiguous |= state not in {"open", "closed"}
        else:
            raise ValueError("unsupported port element")
    if len(seen) + summarized != 65535:
        raise ValueError("missing or excessive port coverage")
    return tuple(sorted(unexpected)), ambiguous


class NmapTCPScanner:
    """Fixed connect scan. The qualified binary and parent are trusted/immutable.

    No NSE, scripts, discovery, DNS, UDP, shell, credential environment or user
    arguments. Qualification must pin the tool image and dependent data too.
    """

    def __init__(self, executable: Path, sha256: str, version: str, *, timeout=120):
        if (
            not isinstance(executable, Path)
            or not executable.is_absolute()
            or not isinstance(sha256, str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", sha256)
            or not isinstance(version, str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+", version)
            or type(timeout) is not int
            or not 1 <= timeout <= 300
        ):
            raise ValueError("qualified tool configuration required")
        self.executable, self.sha256, self.version, self.timeout = (
            executable,
            sha256,
            version,
            timeout,
        )

    def __call__(self, host: ScanHost) -> bytes:
        if not isinstance(host, ScanHost) or self.executable.is_symlink():
            raise ValueError("unregistered host or scanner")
        with tempfile.TemporaryDirectory(prefix="bravo-nmap-") as temporary:
            root = Path(temporary)
            binary = root / "nmap"
            shutil.copyfile(self.executable, binary)
            if digest(binary.read_bytes()) != self.sha256:
                raise ValueError("scanner digest mismatch")
            binary.chmod(0o700)
            env = {"HOME": str(root), "TMPDIR": str(root), "LANG": "C", "LC_ALL": "C"}
            report = root / "output.xml"
            command = [
                str(binary),
                "--unprivileged",
                "-sT",
                "-Pn",
                "-n",
                "-p",
                "1-65535",
                "--max-retries",
                "1",
                "--max-rate",
                "1000",
                "--host-timeout",
                f"{self.timeout}s",
                "--no-stylesheet",
                "-oX",
                "-",
                host.ip,
            ]
            with report.open("xb") as output:
                report.chmod(0o600)
                process = subprocess.Popen(
                    command,
                    cwd=root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + self.timeout + 5
                try:
                    while process.poll() is None:
                        if report.stat().st_size > MAX_XML:
                            raise ValueError("scan output budget exceeded")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("scan deadline exceeded")
                        time.sleep(0.05)
                    if process.returncode != 0:
                        raise ValueError("scanner failed")
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            if report.stat().st_size > MAX_XML:
                raise ValueError("scan output budget exceeded")
            raw = report.read_bytes()
            parse_scan(raw, host, self.version)
            return raw


class PublicPortsProbe:
    """Trusted worker calls live inventory/ingress readers, never JSON claims.

    read_inventory(context, external_agent) must attest complete approved scope
    and the worker's external network placement on each call. check_ingress must
    issue a fresh HTTPS request from this same worker and validate TLS/response.
    """

    def __init__(
        self,
        target,
        *,
        scanner,
        read_inventory,
        check_ingress,
        evidence_sink,
        clock=lambda: datetime.now(UTC),
    ):
        if (
            not isinstance(target, PublicScanTarget)
            or not isinstance(scanner, NmapTCPScanner)
            or not all(callable(x) for x in (read_inventory, check_ingress, evidence_sink, clock))
        ):
            raise ValueError("trusted probe configuration required")
        self.target, self.scanner = target, scanner
        self.inventory, self.ingress, self.sink, self.clock = (
            read_inventory,
            check_ingress,
            evidence_sink,
            clock,
        )

    def __call__(self, context, spec):
        if (
            context.app != self.target.app
            or context.environment != self.target.environment
            or spec != SPEC
        ):
            raise ValueError("probe scope mismatch")
        binding = self.target.binding

        def inventory():
            if self.inventory(context, self.target.external_agent) != self.target:
                raise ValueError("external inventory changed or unverified")

        def positive():
            nonce = str(uuid4())
            started = _utc(self.clock())
            receipt = self.ingress(context, self.target, nonce)
            now = _utc(self.clock())
            if (
                not isinstance(receipt, IngressReceipt)
                or receipt.context != context
                or receipt.binding != binding
                or receipt.nonce != nonce
                or receipt.agent != self.target.external_agent
                or receipt.ingress_ref != self.target.ingress_ref
                or type(receipt.responded) is not bool
                or not started <= _utc(receipt.observed_at) <= now
                or (now - _utc(receipt.observed_at)).total_seconds() > 10
            ):
                raise ValueError("ingress receipt invalid")
            return receipt.responded

        inventory()
        healthy_before = positive()
        summaries, unknown, unexpected = [], False, False
        # No network scanning if the external positive control is unavailable.
        if healthy_before:
            for host in self.target.hosts:
                raw = self.scanner(host)
                opens, ambiguous = parse_scan(raw, host, self.scanner.version)
                unexpected |= bool(opens)
                unknown |= ambiguous
                summaries.append(
                    {
                        "host_uid": host.uid,
                        "output_sha256": digest(raw),
                        "unexpected_open_ports": opens,
                        "ambiguous": ambiguous,
                    }
                )
            healthy_after = positive()
        else:
            healthy_after = False
        inventory()
        healthy = healthy_before and healthy_after
        reason = (
            "POSITIVE_CONTROL_FAILED" if not healthy else "UNEXPECTED_RESPONSE" if unknown else None
        )
        # A verified unexpected open is FAIL even if other ports are ambiguous.
        observed = (
            None if not healthy else "present" if unexpected else None if unknown else "absent"
        )
        if observed is not None:
            reason = None
        payload = canonical(
            {
                "run_id": context.run_id,
                "app": context.app,
                "environment": asdict(context.environment),
                "test_id": spec.test_id,
                "binding": binding,
                "protocol": "tcp",
                "port_range": "1-65535",
                "tool_sha256": self.scanner.sha256,
                "tool_version": self.scanner.version,
                "results": summaries,
                "observed": observed,
                "unknown_reason": reason,
                "positive_control": "pass" if healthy else "fail",
            }
        )
        self.sink(payload)
        return Observation(
            context,
            spec.test_id,
            spec.method_version,
            observed,
            "pass" if healthy else "fail",
            _utc(self.clock()),
            "nmap",
            self.scanner.version,
            digest(payload),
            reason,
        )
