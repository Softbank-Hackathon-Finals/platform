import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from bravo_controls.public_ports import (
    MAX_XML,
    SPEC,
    IngressReceipt,
    NmapTCPScanner,
    PublicPortsProbe,
    PublicScanTarget,
    ScanHost,
    digest,
    parse_scan,
)
from bravo_controls.runner import Environment, RunContext

HOST = ScanHost("node-a", "192.0.2.1", frozenset({443}))
ENV = Environment("source", "source", "test/source")
CTX = RunContext("run-public", "chat", ENV)
TARGET = PublicScanTarget("chat", ENV, "external-worker", (HOST,), "public-ingress", "revision-1")
NOW = datetime.now(UTC)


def report(*, state="closed", count=65534, port=443, explicit="open", version="7.95"):
    return f'''<?xml version="1.0"?>
<!DOCTYPE nmaprun>
<nmaprun scanner="nmap" version="{version}">
<scaninfo type="connect" protocol="tcp" numservices="65535" services="1-65535"/>
<host><status state="up"/><address addr="192.0.2.1" addrtype="ipv4"/>
<ports><extraports state="{state}" count="{count}"/>
<port protocol="tcp" portid="{port}"><state state="{explicit}"/></port></ports></host>
<runstats><finished exit="success"/><hosts up="1" down="0" total="1"/></runstats>
</nmaprun>'''.encode()


def test_complete_allowed_scan():
    assert parse_scan(report(), HOST, "7.95") == ((), False)


def test_unexpected_open_and_filtered_are_separate():
    assert parse_scan(report(state="filtered", port=5432), HOST, "7.95") == ((5432,), True)


@pytest.mark.parametrize(
    "old,new",
    [
        (b'numservices="65535"', b'numservices="1000"'),
        (b'services="1-65535"', b'services="1-1000"'),
        (b'type="connect"', b'type="syn"'),
        (b'protocol="tcp"', b'protocol="udp"'),
        (b'count="65534"', b'count="65533"'),
        (b'count="65534"', b'count="65535"'),
        (b'count="65534"', b'count="-1"'),
        (b'count="65534"', b'count="garbage"'),
        (b'extraports state="closed"', b'extraports state="open"'),
        (b'portid="443"', b'portid="0"'),
        (b'portid="443"', b'portid="65536"'),
        (b'addr="192.0.2.1"', b'addr="192.0.2.2"'),
        (b'state="up"', b'state="down"'),
        (b"<host>", b'<host timedout="true">'),
        (b'exit="success"', b'exit="error"'),
        (b'total="1"', b'total="2"'),
        (b'version="7.95"', b'version="7.94"'),
        (b"<runstats>", b"<missing>"),
        (b'<state state="open"/>', b'<state state="unknown"/>'),
        (b'<state state="open"/>', b'<state state="open"/><state state="closed"/>'),
        (b"</ports>", b'<port protocol="tcp" portid="443"><state state="closed"/></port></ports>'),
        (b"</host>", b"</host><host/>"),
        (b"<!DOCTYPE nmaprun>", b'<!DOCTYPE nmaprun SYSTEM "file:///etc/passwd">'),
        (b"<!DOCTYPE nmaprun>", b'<!DOCTYPE nmaprun [<!ENTITY x "payload">]>'),
    ],
)
def test_malformed_incomplete_foreign_scan_rejected(old, new):
    with pytest.raises((ValueError, ET.ParseError)):
        parse_scan(report().replace(old, new), HOST, "7.95")


@pytest.mark.parametrize(
    "raw", [b"", b"x" * (MAX_XML + 1), b"\x00", b"\xff", report().decode().encode("utf-16")]
)
def test_output_bounds_and_encoding(raw):
    with pytest.raises((ValueError, UnicodeError)):
        parse_scan(raw, HOST, "7.95")


@pytest.fixture
def setup(tmp_path):
    binary = tmp_path / "nmap"
    binary.write_bytes(b"qualified test binary")
    scanner = NmapTCPScanner(binary, digest(binary.read_bytes()), "7.95")
    evidence = []

    def positive(ctx, target, nonce):
        return IngressReceipt(
            ctx, target.binding, nonce, target.external_agent, target.ingress_ref, True, NOW
        )

    probe = PublicPortsProbe(
        TARGET,
        scanner=scanner,
        read_inventory=lambda *_: TARGET,
        check_ingress=positive,
        evidence_sink=evidence.append,
        clock=lambda: NOW,
    )
    return probe, evidence


@pytest.mark.parametrize(
    "xml,expected,reason",
    [
        (report(), "absent", None),
        (report(port=22), "present", None),
        (report(state="filtered"), None, "UNEXPECTED_RESPONSE"),
        (report(state="filtered", port=5432), "present", None),
    ],
)
def test_probe_outcomes_and_private_summary(setup, xml, expected, reason):
    probe, evidence = setup
    with patch.object(NmapTCPScanner, "__call__", return_value=xml):
        result = probe(CTX, SPEC)
    assert result.observed == expected and result.unknown_reason == reason
    assert result.positive_control == "pass"
    assert result.output_sha256 == digest(evidence[0])
    assert b"192.0.2.1" not in evidence[0] and b"<nmaprun" not in evidence[0]
    assert json.loads(evidence[0])["binding"] == TARGET.binding


def test_failed_positive_skips_scan(setup):
    probe, _ = setup
    original = probe.ingress
    probe.ingress = lambda *args: replace(original(*args), responded=False)
    with patch.object(NmapTCPScanner, "__call__") as scan:
        result = probe(CTX, SPEC)
    scan.assert_not_called()
    assert result.observed is None and result.unknown_reason == "POSITIVE_CONTROL_FAILED"


def test_positive_fails_after_scan(setup):
    probe, _ = setup
    original, count = probe.ingress, 0

    def ingress(*args):
        nonlocal count
        count += 1
        return replace(original(*args), responded=count == 1)

    probe.ingress = ingress
    with patch.object(NmapTCPScanner, "__call__", return_value=report()):
        result = probe(CTX, SPEC)
    assert result.observed is None and result.positive_control == "fail"


@pytest.mark.parametrize(
    "changes",
    [
        {"nonce": "replay"},
        {"agent": "internal-worker"},
        {"binding": "wrong"},
        {"ingress_ref": "other-ingress"},
        {"context": replace(CTX, run_id="foreign")},
        {"responded": 1},
        {"observed_at": NOW - timedelta(seconds=1)},
        {"observed_at": NOW + timedelta(seconds=1)},
    ],
)
def test_receipt_binding_and_freshness(setup, changes):
    probe, evidence = setup
    original = probe.ingress
    probe.ingress = lambda *args: replace(original(*args), **changes)
    with pytest.raises(ValueError, match="receipt"):
        probe(CTX, SPEC)
    assert not evidence


def test_inventory_changes_after_scan(setup):
    probe, evidence = setup
    calls = iter([TARGET, replace(TARGET, inventory_revision="revision-2")])
    probe.inventory = lambda *_: next(calls)
    with patch.object(NmapTCPScanner, "__call__", return_value=report()):
        with pytest.raises(ValueError, match="inventory"):
            probe(CTX, SPEC)
    assert not evidence


def test_sink_failure_cannot_return_pass(setup):
    probe, _ = setup

    def fail(_):
        raise PermissionError("unavailable")

    probe.sink = fail
    with patch.object(NmapTCPScanner, "__call__", return_value=report()):
        with pytest.raises(PermissionError):
            probe(CTX, SPEC)


def test_scope_mismatch_prevents_scan(setup):
    probe, _ = setup
    with pytest.raises(ValueError, match="scope"):
        probe(replace(CTX, app="other"), SPEC)


@pytest.mark.parametrize(
    "ip",
    [
        "example.com",
        "192.0.2.1/24",
        "--script=all",
        "::1",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
    ],
)
def test_target_not_arbitrary_scanner_argument(ip):
    with pytest.raises(ValueError):
        ScanHost("host", ip, frozenset())


@pytest.mark.parametrize("ports", [frozenset({True}), frozenset({0}), frozenset({65536}), [443]])
def test_invalid_allowlist(ports):
    with pytest.raises(ValueError):
        ScanHost("host", "127.0.0.1", ports)


def test_empty_or_duplicate_inventory():
    for hosts in ((), (HOST, HOST)):
        with pytest.raises(ValueError):
            replace(TARGET, hosts=hosts)


def test_scanner_digest_mismatch_prevents_execution(setup):
    probe, _ = setup
    probe.scanner.executable.write_bytes(b"replaced")
    with patch("bravo_controls.public_ports.subprocess.Popen") as launch:
        with pytest.raises(ValueError, match="digest"):
            probe.scanner(HOST)
    launch.assert_not_called()


def test_real_subprocess_fixed_arguments_and_scrubbed_environment(tmp_path, monkeypatch):
    # Exercise process lifecycle with a harmless executable; actual Nmap has its
    # own disposable-container qualification, not replaced by this fixture.
    import sys

    binary = tmp_path / "nmap"
    expected = repr(report())
    binary.write_text(f"""#!{sys.executable}
import os, sys
assert not any(k.startswith(('AWS_', 'NMAP_', 'LD_')) for k in os.environ)
assert sys.argv[1:] == {repr(["--unprivileged", "-sT", "-Pn", "-n", "-p", "1-65535", "--max-retries", "1", "--max-rate", "1000", "--host-timeout", "120s", "--no-stylesheet", "-oX", "-", HOST.ip])}
sys.stdout.buffer.write({expected})
""")
    binary.chmod(0o700)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-forward")
    scanner = NmapTCPScanner(binary, digest(binary.read_bytes()), "7.95")
    assert scanner(HOST) == report()


def test_process_error_has_no_raw_output(tmp_path):
    binary = tmp_path / "bad"
    binary.write_text("#!/bin/sh\necho secret >&2\nexit 1\n")
    binary.chmod(0o700)
    scanner = NmapTCPScanner(binary, digest(binary.read_bytes()), "7.95")
    with pytest.raises(ValueError, match="^scanner failed$"):
        scanner(HOST)


def test_t5_runner_accepts_live_bound_observation(setup):
    from pathlib import Path

    from bravo_controls.runner import Catalog, Runner

    probe, _ = setup
    controls = Path(__file__).resolve().parents[3] / "docs" / "controls"
    catalog = Catalog(
        json.loads((controls / "profiles.json").read_text()),
        json.loads((controls / "control-result.schema.json").read_text()),
    )
    probe.clock = lambda: datetime.now(UTC)
    original = probe.ingress
    probe.ingress = lambda *args: replace(original(*args), observed_at=datetime.now(UTC))
    with patch.object(NmapTCPScanner, "__call__", return_value=report()):
        run = Runner(catalog, {"NET-01.1": probe}).run(
            profile_id="full", profile_version="1", app=TARGET.app, environment=ENV
        )
    rows = {r["test_id"]: r for r in run["results"]}
    assert rows["NET-01.1"]["verdict"] == "PASS"
    assert all(r["verdict"] == "UNKNOWN" for k, r in rows.items() if k != "NET-01.1")


def test_pre_scan_receipt_cannot_be_replayed_after_scan(setup):
    probe, evidence = setup
    original, saved = probe.ingress, []

    def replay(*args):
        if not saved:
            saved.append(original(*args))
        return saved[0]

    probe.ingress = replay
    with patch.object(NmapTCPScanner, "__call__", return_value=report()):
        with pytest.raises(ValueError, match="receipt"):
            probe(CTX, SPEC)
    assert not evidence


def test_all_inventory_addresses_must_complete(setup):
    probe, evidence = setup
    second = ScanHost("node-b", "192.0.2.2", frozenset({443}))
    probe.target = replace(TARGET, hosts=(HOST, second))
    probe.inventory = lambda *_: probe.target
    with patch.object(
        NmapTCPScanner, "__call__", side_effect=[report(), TimeoutError("private")]
    ) as scan:
        with pytest.raises(TimeoutError):
            probe(CTX, SPEC)
    assert scan.call_count == 2 and not evidence


def test_summary_cannot_double_count_closed_ports():
    raw = report(count=32767).replace(
        b"</ports>", b'<extraports state="closed" count="32767"/></ports>'
    )
    with pytest.raises(ValueError, match="duplicate summary"):
        parse_scan(raw, HOST, "7.95")


def test_timeout_kills_and_reaps_process(setup):
    probe, _ = setup
    from unittest.mock import MagicMock

    process = MagicMock()
    process.poll.return_value = None
    with (
        patch("bravo_controls.public_ports.subprocess.Popen", return_value=process),
        patch("bravo_controls.public_ports.time.monotonic", side_effect=[0, 126]),
    ):
        with pytest.raises(TimeoutError):
            probe.scanner(HOST)
    process.kill.assert_called_once()
    process.wait.assert_called_once()


def test_oversized_output_kills_and_reaps_process(setup):
    probe, _ = setup
    from unittest.mock import MagicMock

    process = MagicMock()
    process.poll.return_value = None

    def launch(_command, **kwargs):
        kwargs["stdout"].truncate(MAX_XML + 1)
        return process

    with patch("bravo_controls.public_ports.subprocess.Popen", side_effect=launch):
        with pytest.raises(ValueError, match="budget"):
            probe.scanner(HOST)
    process.kill.assert_called_once()
    process.wait.assert_called_once()
