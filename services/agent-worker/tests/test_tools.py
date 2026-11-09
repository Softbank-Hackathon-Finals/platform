import hashlib
import json
import os

import pytest
from pydantic import ValidationError

from bravo_agent.contracts import FileEdit, Proposal, SnapshotManifest
from bravo_agent.tools import PolicyDenied


def test_only_approved_snapshot_and_schema_proposal(context):
    _, snapshot, proposal, tools = context
    response = tools.dispatch("mcp__bravo__repo_read", {"path": "src/config.py"})
    assert response["content"] == 'DB_HOST = "localhost"\n'
    assert response["sha256"] == snapshot.manifest.files["src/config.py"]
    assert tools.events[-1]["content_digest"] == response["sha256"]
    response = tools.dispatch("mcp__bravo__plan_propose", proposal)
    assert response["status"] == "PROPOSAL_RECEIVED"
    assert len(response["proposal_digest"]) == 64
    assert (snapshot.root / "src/config.py").read_text() == 'DB_HOST = "localhost"\n'


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "src/../config.py", ".env",
                                 "src/.env", "tests/hidden.py", ".github/workflows/ci.yml",
                                 "src/test_hidden.py", "src/auth.test.ts", "src/auth_test.py",
                                 "src/secret.json", "src/credentials.json", "src/private.key"])
def test_forbidden_paths_never_enter_snapshot_manifest(path):
    with pytest.raises(ValidationError):
        SnapshotManifest(source_sha="1" * 40, files={path: "a" * 64}, editable_paths=[path])


@pytest.mark.parametrize("name", ["Bash", "Read", "Write", "Agent", "Skill", "WebFetch",
                                 "rehearsal.exec", "mcp__other__repo_read", "kubectl delete namespace"])
def test_unlisted_tools_are_denied_without_logging_arguments(context, name):
    tools = context[3]
    with pytest.raises(PolicyDenied):
        tools.dispatch(name, {"secret": "do-not-record-this"})
    assert tools.events[-1]["outcome"] == "DENIED"
    assert "do-not-record-this" not in json.dumps(tools.events)


@pytest.mark.parametrize("mutation", ["symlink", "parent_symlink", "hardlink", "changed"])
def test_snapshot_cannot_escape_or_change_after_manifest(context, tmp_path, mutation):
    _, snapshot, _, tools = context
    path = snapshot.root / "src/config.py"
    original = path.read_text()
    outside = tmp_path / "outside.py"
    outside.write_text(original)
    if mutation == "symlink":
        path.unlink()
        path.symlink_to(outside)
    elif mutation == "parent_symlink":
        path.unlink()
        path.parent.rmdir()
        target = tmp_path / "outside"
        target.mkdir()
        (target / "config.py").write_text(original)
        path.parent.symlink_to(target, target_is_directory=True)
    elif mutation == "hardlink":
        path.unlink()
        os.link(outside, path)
    else:
        path.write_text("changed\n")
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__repo_read", {"path": "src/config.py"})
    assert tools.events[-1]["outcome"] == "DENIED"


def test_secret_content_denied_even_if_manifest_digest_matches(context):
    _, snapshot, _, tools = context
    content = "sk-" + "ant-" + "a" * 32
    (snapshot.root / "src/config.py").write_text(content)
    snapshot.manifest.files["src/config.py"] = hashlib.sha256(content.encode()).hexdigest()
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__repo_read", {"path": "src/config.py"})
    assert content not in json.dumps(tools.events)


@pytest.mark.parametrize("field", ["rationale", "replacement"])
def test_model_cannot_copy_recognized_secrets_into_proposal(context, field):
    proposal, tools = context[2:]
    value = "sk-" + "ant-" + "a" * 32
    if field == "rationale":
        proposal[field] = value
    else:
        proposal["edits"][0][field] = value
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__plan_propose", proposal)
    assert value not in json.dumps(tools.events)
    assert tools.proposal is None


@pytest.mark.parametrize("content", ["rm -rf /", "DROP TABLE users", "TRUNCATE x",
                                    "terraform destroy", "kubectl delete namespace production",
                                    "aws s3 rb s3://bucket", '{"Action":"*"}', "delete_bucket(x)"])
def test_destructive_proposal_is_rejected_and_audited(context, content):
    proposal, tools = context[2:]
    proposal["edits"][0]["replacement"] = content
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__plan_propose", proposal)
    assert tools.proposal is None
    assert tools.events[-1]["outcome"] == "DENIED"


@pytest.mark.parametrize("change", ["extra", "revision", "run", "digest", "path", "too_large"])
def test_invalid_proposals_cannot_be_accepted(context, change):
    proposal, tools = context[2:]
    if change == "extra":
        proposal["verdict"] = "PASS"
    elif change == "revision":
        proposal["source_sha"] = "2" * 40
    elif change == "run":
        proposal["run_id"] = "another-run"
    elif change == "digest":
        proposal["edits"][0]["before_sha256"] = "b" * 64
    elif change == "path":
        proposal["edits"][0]["path"] = "src/other.py"
    else:
        proposal["edits"][0]["replacement"] = "x" * 32769
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__plan_propose", proposal)
    assert tools.proposal is None


def test_schema_forbids_execution_fields_and_limits_edits():
    assert Proposal.model_json_schema()["additionalProperties"] is False
    assert FileEdit.model_json_schema()["additionalProperties"] is False
    assert "command" not in FileEdit.model_fields
    assert "verdict" not in Proposal.model_fields


def test_duplicate_proposals_and_tool_flood_are_bounded(context):
    proposal, tools = context[2:]
    tools.dispatch("mcp__bravo__plan_propose", proposal)
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__plan_propose", proposal)
    for _ in range(150):
        with pytest.raises(PolicyDenied):
            tools.dispatch("Bash", {"command": "echo x"})
    assert len(tools.events) == 128
    assert tools.event_count > len(tools.events)
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__repo_read", {"path": "src/config.py"})
