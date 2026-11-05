"""Explicit integration command; requires real T24 and T11 on PYTHONPATH.
No import skip and no fake ledger. Local FileSink is NOT production Object Lock.
Run: python -m pytest tests/interop_audit.py -q
"""

import hashlib
import json
from dataclasses import replace

import pytest
from bravo_agent.contracts import FailureBundle, SnapshotManifest
from bravo_agent.tools import PolicyDenied, Snapshot, Tools
from bravo_audit.adapters import from_agent_tool_event
from bravo_audit.ledger import RunLedger, verify_chain
from bravo_audit.sink import FileSink
from fastapi.testclient import TestClient
from test_app import ORIGIN, setup

from bravo_console.audit import RegisteredAuditSnapshot, t24_reader


@pytest.mark.parametrize("mode", ["healthy", "tampered", "wrong_anchor", "replay"])
def test_actual_t11_t24_t21(tmp_path, mode):
    content = b"DB_HOST = 'localhost'\n"
    root = tmp_path / "source"
    root.mkdir()
    (root / "config.py").write_bytes(content)
    bundle = FailureBundle(
        run_id="run-interop",
        source_sha="1" * 40,
        failure_code="DB_CONNECTION_FAILED",
        summary="Destination DB unavailable",
        evidence_digest="a" * 64,
    )
    manifest = SnapshotManifest(
        source_sha=bundle.source_sha,
        files={"config.py": hashlib.sha256(content).hexdigest()},
        editable_paths=["config.py"],
    )
    tools = Tools(bundle, Snapshot(root, manifest), 1)
    command = "aws s3 rb s3://test-attachments --force"
    with pytest.raises(PolicyDenied):
        tools.dispatch("rehearsal.exec", {"command": command})
    assert command not in json.dumps(tools.events)
    plan, run, source, kind, origin, payload = from_agent_tool_event(
        tools.events[0], "plan-demo", 0
    )
    ledger = RunLedger(plan, run)
    record, created = ledger.append(source, kind, payload, origin)
    assert created
    sink = FileSink(tmp_path / "stored")
    receipt = sink.put(record)
    assert receipt.version_id == "file"
    records = [json.loads((sink.root / receipt.key).read_bytes())]
    assert verify_chain(records)[0]
    if mode == "replay":
        _, created = ledger.append(source, kind, payload, origin)
        assert not created and len(ledger.records) == 1
    if mode == "tampered":
        records[0]["payload"]["tool"] = "unlisted"
    app, store, _, _ = setup(tmp_path / "plan.db")
    registered = RegisteredAuditSnapshot(
        plan,
        store.snapshot(plan)["plan_digest"],
        run,
        1,
        record.sha256,
        json.dumps({"records": records}).encode(),
    )
    if mode == "wrong_anchor":
        registered = replace(registered, chain_digest="f" * 64)

    async def load(scope, snapshot):
        return registered

    async def authenticate(scope, snapshot, supplied):
        return supplied is registered

    app.audit = t24_reader(app.api, load=load, authenticate=authenticate)
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-session": "test-only"}) as c:
        result = c.get("/v1/plans/plan-demo/audit")
    if mode in ("tampered", "wrong_anchor"):
        assert result.status_code == 503
    else:
        assert result.status_code == 200, result.text
        row = result.json()["rows"][0]
        assert row["reason"] == kind and row["category"] == "bucket_delete"
        assert row["record_id"] == record.record_id
        assert command not in result.text and not result.json()["retention_verified"]
