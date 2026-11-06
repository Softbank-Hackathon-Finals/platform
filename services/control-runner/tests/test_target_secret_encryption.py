import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bravo_controls.runner import Environment, RunContext
from bravo_controls.target_secret_encryption import (
    RKE2_VERSION,
    SPEC,
    LocalRKE2StatusReader,
    TargetEncryptionRegistration,
    TargetSecretEncryptionProbe,
    _bao,
    _load,
    _rke2,
)
from bravo_controls.tool_reports import ToolRun

NOW = datetime(2026, 11, 6, tzinfo=UTC)
ENV = Environment("target-example", "target", "local:target-example")
TARGET = TargetEncryptionRegistration(
    "chat", ENV, "cluster", ("node-a", "node-b"),
    "b76fd1f1-9b03-40f1-bd62-1d4f449e4192", "2.7.2+bravo.3", "pebbledb",
    "bao-pod", "volume", "app-secret", "delivery-1",
)
CONTEXT = RunContext("encryption-example", TARGET.app, ENV)
CANARY = "private-response-must-never-escape"


def rke2(**kw):
    return {"enable": True, "stage": "start", "hashmatch": True,
            "activekey": "AES-CBC aescbckey", **kw}


def bao(**kw):
    return {"initialized": True, "sealed": False, "migration": False,
            "cluster_id": TARGET.bao_cluster_id, "version": TARGET.bao_version,
            "storage_type": TARGET.bao_storage, "type": "shamir", "recovery_seal": False,
            "t": 1, "n": 1, "progress": 0, **kw}


def tool(node, payload=None):
    return ToolRun("rke2-secrets-encrypt", RKE2_VERSION, CONTEXT.run_id, node,
                   json.dumps(rke2() if payload is None else payload).encode(), 0, True)


def probe(*, registration=None, nodes=None, seal=None, sink=None, clock=lambda: NOW,
          monotonic=lambda: 1):
    evidence, calls = [], []

    def read_node(ctx, target, uid):
        calls.append(uid)
        return tool(uid)

    value = TargetSecretEncryptionProbe(
        TARGET, read_registration=registration or (lambda *_: TARGET.binding()),
        read_node_status=nodes or read_node, read_bao_status=seal or bao,
        evidence_sink=sink or evidence.append, clock=clock, monotonic=monotonic,
    )
    return value, evidence, calls


def test_checks_every_registered_node_twice_and_retains_only_sanitized_metadata():
    value, evidence, calls = probe(seal=lambda: bao(nonce=CANARY, cluster_name=CANARY))
    result = value(CONTEXT, SPEC)
    assert result.observed == "enabled"
    assert calls == ["node-a", "node-b", "node-a", "node-b"]
    raw = evidence[0].decode()
    assert CANARY not in raw and "aescbckey" not in raw and TARGET.bao_cluster_id not in raw
    assert json.loads(raw)["facts"]["metadata_only"] is True


def test_explicitly_disabled_rke2_is_not_passed_by_a_healthy_vault():
    value, _, _ = probe(nodes=lambda c, t, n: tool(n, rke2(enable=False, activekey="")))
    assert value(CONTEXT, SPEC).observed == "disabled"


@pytest.mark.parametrize("field,new", [
    ("enable", "true"), ("enable", 1), ("enable", None), ("hashmatch", False),
    ("hashmatch", 1), ("hasherror", CANARY), ("stage", "prepare"), ("stage", "rotate"),
    ("stage", "reencrypt_active"), ("activekey", ""), ("activekey", "unknown key"),
    ("inactivekeys", CANARY), ("inactivekeys", [None]), ("inactivekeys", ["key"] * 33),
    ("encryption_key", CANARY),
])
def test_ambiguous_rke2_metadata_is_unknown_without_raw_values(field, new):
    value, evidence, _ = probe(nodes=lambda c, t, n: tool(n, rke2(**{field: new})))
    result = value(CONTEXT, SPEC)
    assert result.observed is None and result.unknown_reason == "UNEXPECTED_RESPONSE"
    assert CANARY not in repr(evidence)


@pytest.mark.parametrize("field,new", [
    ("initialized", False), ("initialized", 1), ("sealed", True), ("sealed", 0),
    ("migration", True), ("migration", 0), ("recovery_seal", True), ("recovery_seal", 0),
    ("cluster_id", "another"), ("version", "2.7.1"), ("storage_type", "inmem"),
    ("type", "awskms"), ("t", 0), ("t", True), ("n", 0), ("n", True),
    ("progress", 1), ("progress", False),
])
def test_unavailable_or_different_seal_metadata_never_implies_plaintext(field, new):
    value, evidence, _ = probe(seal=lambda: bao(**{field: new}, nonce=CANARY))
    result = value(CONTEXT, SPEC)
    assert result.observed is None and result.unknown_reason == "UNEXPECTED_RESPONSE"
    assert CANARY not in repr(evidence)


@pytest.mark.parametrize("field,new", [
    ("tool", "fake"), ("version", "v1.35.8+rke2r1"), ("scope", "stale-run"),
    ("target", "node-c"), ("exit_code", 1), ("exit_code", False), ("complete", False),
    ("complete", 1), ("output", b'{"enable":true,"enable":false}'),
    ("output", b'{}'), ("output", b'{"enable":NaN}'), ("output", b'x' * 16385),
])
def test_incomplete_unbound_or_non_json_status_rejected(field, new):
    value, _, _ = probe(nodes=lambda c, t, n: replace(tool(n), **{field: new}))
    assert value(CONTEXT, SPEC).observed is None


def test_node_key_drift_and_second_registration_drift_are_unknown():
    value, _, _ = probe(nodes=lambda c, t, n: tool(n, rke2(activekey="AES-CBC " + n)))
    assert value(CONTEXT, SPEC).observed is None
    bindings = iter([TARGET.binding(), {}])
    value, _, _ = probe(registration=lambda *_: next(bindings))
    assert value(CONTEXT, SPEC).observed is None


def test_runtime_transition_between_reads_is_unknown():
    outputs = iter([tool("node-a"), tool("node-b"),
                    tool("node-a", rke2(stage="reencrypt_finished")), tool("node-b")])
    value, _, _ = probe(nodes=lambda *_: next(outputs))
    assert value(CONTEXT, SPEC).observed is None


@pytest.mark.parametrize("error,reason", [(PermissionError, "OBSERVATION_DENIED"),
                                         (TimeoutError, "TIMEOUT"), (OSError, "TOOL_ERROR")])
def test_collector_errors_are_not_evidence(error, reason):
    def fail(*_):
        raise error(CANARY)

    value, evidence, _ = probe(nodes=fail)
    result = value(CONTEXT, SPEC)
    assert result.observed is None and result.unknown_reason == reason
    assert CANARY not in repr(evidence)


def test_evidence_storage_failure_cannot_return_success():
    def fail(_):
        raise OSError("storage unavailable")

    value, _, _ = probe(sink=fail)
    with pytest.raises(OSError):
        value(CONTEXT, SPEC)


def test_expired_budget_and_wall_clock_rollback_are_unknown():
    times = iter([1, 61])
    value, _, _ = probe(monotonic=lambda: next(times))
    assert value(CONTEXT, SPEC).unknown_reason == "TIMEOUT"
    times = iter([NOW, NOW - timedelta(seconds=1), NOW])
    value, _, _ = probe(clock=lambda: next(times))
    assert value(CONTEXT, SPEC).observed is None


def test_wrong_app_environment_or_spec_never_calls_collectors():
    value, _, calls = probe()
    with pytest.raises(ValueError):
        value(replace(CONTEXT, app="other"), SPEC)
    with pytest.raises(ValueError):
        value(CONTEXT, replace(SPEC, test_id="ENC-01.1"))
    assert not calls


def test_local_reader_uses_fixed_no_write_commands_and_no_inherited_credentials(tmp_path, monkeypatch):
    import hashlib

    from bravo_controls import target_secret_encryption as module

    binary = tmp_path / "rke2"
    binary.write_bytes(b"qualified-test-binary")
    reader = LocalRKE2StatusReader(binary, binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                                  node_uid="node-a")
    calls = []

    def execute(command, **kw):
        calls.append(command)
        assert kw["env"] == {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                             "LANG": "C", "LC_ALL": "C", "HOME": "/nonexistent"}
        assert kw["cwd"] == "/" and kw["timeout"] <= 5
        if command[-1] == "--version":
            return 0, f"rke2 version {RKE2_VERSION} (abc123)\ngo version go1.25\n".encode(), b""
        return 0, json.dumps(rke2()).encode(), b""

    monkeypatch.setattr(module, "_execute", execute)
    result = reader(CONTEXT, TARGET, "node-a")
    assert _rke2(result, CONTEXT, "node-a")["enable"]
    assert calls == [[str(binary), "--version"],
                     [str(binary), "secrets-encrypt", "status", "--output", "json"]]
    binary.write_bytes(b"changed")
    with pytest.raises(ValueError):
        reader(CONTEXT, TARGET, "node-a")
    assert len(calls) == 2


def test_metadata_parsers_reject_missing_keys_and_duplicate_fields():
    with pytest.raises(ValueError):
        _load(b'{"sealed":true,"sealed":false}')
    for key in ("t", "n", "initialized", "cluster_id", "storage_type", "migration"):
        body = bao()
        del body[key]
        with pytest.raises(ValueError):
            _bao(body, TARGET)
