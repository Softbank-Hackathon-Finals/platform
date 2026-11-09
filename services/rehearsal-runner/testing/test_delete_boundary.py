"""Cross-service check of T11's destructive-attempt record (design 8.4 demo scene)."""

import hashlib
import json

import pytest
from bravo_agent.contracts import FailureBundle, SnapshotManifest
from bravo_agent.tools import PolicyDenied, Snapshot, Tools


def tools_for(tmp_path):
    (tmp_path / "src").mkdir()
    source = "configuration = True\n"
    (tmp_path / "src/config.py").write_text(source)
    source_digest = hashlib.sha256(source.encode()).hexdigest()
    manifest = SnapshotManifest(source_sha="1" * 40, files={"src/config.py": source_digest},
                                editable_paths=["src/config.py"])
    bundle = FailureBundle(run_id="run-1", source_sha="1" * 40,
                           failure_code="POLICY_TRANSLATION", summary="Policy update requested",
                           evidence_digest="a" * 64)
    return Tools(bundle, Snapshot(tmp_path, manifest), 1), source_digest


def test_bucket_deletion_tool_request_is_denied_and_audited(tmp_path):
    tools, _ = tools_for(tmp_path)
    raw_arguments = {"command": "aws s3 rb s3://private-bucket", "marker": "private-argument"}
    with pytest.raises(PolicyDenied):
        tools.dispatch("rehearsal.exec", raw_arguments)
    assert tools.events == [{"run_id": "run-1", "attempt": 1, "tool": "rehearsal.exec",
                             "outcome": "DENIED", "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED",
                             "category": "bucket_delete"}]
    assert "private-argument" not in json.dumps(tools.events)
    assert tools.proposal is None


def test_delete_bucket_in_proposal_is_denied_and_audited(tmp_path):
    tools, source_digest = tools_for(tmp_path)
    proposal = {"run_id": "run-1", "source_sha": "1" * 40, "rationale": "Translate policy",
                "risk": "low", "edits": [{"path": "src/config.py", "before_sha256": source_digest,
                                           "replacement": "client.delete_bucket(Bucket='private-bucket')"}]}
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__plan_propose", proposal)
    assert tools.proposal is None
    assert tools.events[-1]["outcome"] == "DENIED"
    assert tools.events[-1]["reason"] == "DESTRUCTIVE_ATTEMPT_BLOCKED"
    assert tools.events[-1]["category"] == "bucket_delete"
    assert "private-bucket" not in json.dumps(tools.events)
