import hashlib
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from bravo_agent.contracts import FileEdit, InventorySummary, Proposal, SnapshotManifest, safe_path
from bravo_agent.tools import PolicyDenied, Tools


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


# ---- T11 boundary: source path policy, inventory.describe, policy.evaluate, destructive audit


@pytest.mark.parametrize("path", ["app/db_secret.py", "src/secret_store.ts", "pkg/secrets_client.go"])
def test_code_files_named_after_secrets_are_readable_sources(path):
    assert safe_path(path) == path


@pytest.mark.parametrize("path", ["src/secret.json", "config/secret.yaml", "app/db_secret.pem",
                                 "secrets/db.py", "app/credentials_helper.py", "app/private_key.py"])
def test_secret_data_files_and_credential_names_stay_blocked(path):
    with pytest.raises(ValueError):
        safe_path(path)


def test_secret_named_source_still_refuses_literal_credentials(context):
    _, snapshot, _, _ = context
    (snapshot.root / "src/db_secret.py").write_text('password = "hunter2hunter2"\n')
    sha = hashlib.sha256((snapshot.root / "src/db_secret.py").read_bytes()).hexdigest()
    manifest = SnapshotManifest(source_sha="1" * 40, files={"src/db_secret.py": sha}, editable_paths=["src/db_secret.py"])
    tools = Tools(context[0], type(snapshot)(snapshot.root, manifest), 1)
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__repo_read", {"path": "src/db_secret.py"})
    assert "hunter2" not in json.dumps(tools.events)


def _inventory_doc(source_sha="1" * 40):
    return {"schema_version": "1.0", "app": {"id": "demo-chat", "source_sha": source_sha}, "digest": "sha256:" + "c" * 64,
            "deploy": {"workloads": [{"env_sources": {"DB_PASSWORD": "secretKeyRef"}}]},
            "items": [
                {"id": "item-001", "role": "secrets", "service": "secretsmanager", "found_in": "both", "grade": "code_change",
                 "grade_by": "rule", "rationale": "OpenBao+ESO로 바꾸려면 SDK 직접 호출을 주입 방식으로 고쳐야 한다", "agent_needed": False,
                 "risk": "", "code_refs": [{"id": "dep-0003", "kind": "sdk_call", "file": "app/db_secret.py", "line": 12,
                                            "snippet": "<masked: secret-like literal>", "detail": {"factory": "boto3.client"}}],
                 "deploy_refs": [{"kind": "secret", "name": "demo-chat/db", "attributes": {"arn": "arn:aws:secretsmanager:::x.example.invalid"}}],
                 "target": {"env_kind": "onprem", "impl": "OpenBao + ESO", "verify": "다른 SA는 못 읽음"}},
                {"id": "item-002", "role": "object_store", "service": "s3", "found_in": "both", "grade": "config_swap",
                 "grade_by": "rule", "rationale": "endpoint_url이 설정에서 옴", "agent_needed": False, "risk": "",
                 "code_refs": [{"kind": "env_ref", "file": ".env.example", "line": 3}], "target": {"impl": "SeaweedFS"}},
            ]}


def test_inventory_describe_returns_only_the_model_facing_subset(context):
    bundle, snapshot, _, _ = context
    tools = Tools(bundle, snapshot, 1, inventory=InventorySummary.from_document(_inventory_doc()))
    result = tools.dispatch("mcp__bravo__inventory_describe", {"role": "secrets"})
    assert [i["id"] for i in result["items"]] == ["item-001"] and result["inventory_digest"] == "sha256:" + "c" * 64
    dump = json.dumps(result)
    assert "snippet" not in dump and "deploy_refs" not in dump and "arn:aws" not in dump and "DB_PASSWORD" not in dump
    assert result["items"][0]["code_refs"] == [{"file": "app/db_secret.py", "line": 12, "kind": "sdk_call"}]
    assert tools.events[-1]["reason"] == "POLICY_ACCEPTED" and tools.events[-1]["items"] == 1
    everything = tools.dispatch("mcp__bravo__inventory_describe", {})
    assert len(everything["items"]) == 2
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__inventory_describe", {"role": "Secrets; DROP"})


def test_inventory_describe_is_denied_until_a_summary_is_attached(context):
    tools = context[3]
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__inventory_describe", {})
    assert tools.events[-1]["reason"] == "TOOL_NOT_ATTACHED"


def test_inventory_must_match_the_run_revision(context):
    bundle, snapshot, _, _ = context
    with pytest.raises(ValueError):
        Tools(bundle, snapshot, 1, inventory=InventorySummary.from_document(_inventory_doc("2" * 40)))


@pytest.mark.parametrize("text, verdict, reason", [
    ('{"Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::attachments.example.invalid/*"}]}', "PASS", "LINT_PASSED"),
    ('{"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}', "FAIL", "WILDCARD_OR_COMPLEMENT_GRANT"),
    ('{"Statement": [{"Action": "s3:GetObject"}]}', "FAIL", "MISSING_EFFECT"),
    ('{"Statement": [', "FAIL", "AMBIGUOUS_POLICY_JSON"),
    ("aws s3 rb s3://attachments --force", "FAIL", "DESTRUCTIVE_COMMAND"),
    ("sk-" + "ant-" + "a" * 32, "FAIL", "UNSAFE_TEXT"),
])
def test_policy_evaluate_lints_without_side_effects(context, text, verdict, reason):
    proposal, tools = context[2:]
    result = tools.dispatch("mcp__bravo__policy_evaluate", {"policy": text})
    assert (result["verdict"], result["reason"]) == (verdict, reason)
    assert tools.proposal is None and text[:12] not in json.dumps(tools.events)
    if reason == "DESTRUCTIVE_COMMAND":
        assert tools.events[-1]["category"] == "bucket_delete"
    assert tools.dispatch("mcp__bravo__plan_propose", proposal)["status"] == "PROPOSAL_RECEIVED"


@pytest.mark.parametrize("tool, args, category", [
    ("rehearsal.exec", {"command": "aws s3 rb s3://demo-chat-attachments --force"}, "bucket_delete"),
    ("mcp__bravo__rehearsal_exec", {"command": "kubectl delete namespace rehearsal-1"}, "namespace_delete"),
    ("Bash", {"command": "rm -rf /data"}, "filesystem_rm"),
])
def test_destructive_attempts_are_audited_by_category_not_text(context, tool, args, category):
    tools = context[3]
    with pytest.raises(PolicyDenied):
        tools.dispatch(tool, args)
    event = tools.events[-1]
    assert event["outcome"] == "DENIED" and event["reason"] == "DESTRUCTIVE_ATTEMPT_BLOCKED"
    assert event["category"] == category and args["command"] not in json.dumps(tools.events)


def test_reserved_whitelist_tools_are_reported_as_not_provided(context):
    tools = context[3]
    with pytest.raises(PolicyDenied):
        tools.dispatch("rehearsal.run", {"job": "verify"})
    assert tools.events[-1]["reason"] == "TOOL_NOT_PROVIDED" and tools.events[-1]["tool"] == "rehearsal.run"


def test_destructive_proposal_records_category(context):
    proposal, tools = context[2:]
    proposal["edits"][0]["replacement"] = "DROP TABLE messages;"
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__plan_propose", proposal)
    assert tools.events[-1]["reason"] == "DESTRUCTIVE_ATTEMPT_BLOCKED" and tools.events[-1]["category"] == "db_drop"


# ---- review follow-ups (#256): credential literals in newly readable sources, real T16 output


FIXTURES = Path(__file__).parent / "fixtures"


def _snapshot_with(context, name, content):
    _, snapshot, _, _ = context
    path = snapshot.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = SnapshotManifest(source_sha="1" * 40, files={name: sha}, editable_paths=[name])
    return Tools(context[0], type(snapshot)(snapshot.root, manifest), 1)


@pytest.mark.parametrize("literal", [
    'DB_PASSWORD = "{v}"\n',
    'CONFIG = {{"password": "{v}"}}\n',
    'DATABASE_URL = "postgresql://chat:{v}@chat-pooler-rw:5432/chat"\n',
    "db_passwd: {v}\n",
    "AWS_SECRET_ACCESS_KEY={v}\n",
])
def test_credential_literals_in_secret_named_sources_never_reach_the_model(context, literal):
    value = "Synth3ticFixtureValue"
    content = "import os\n" + literal.format(v=value) + 'HOST = os.environ["DB_HOST"]\n'
    tools = _snapshot_with(context, "app/db_secret.py", content)
    with pytest.raises(PolicyDenied):
        tools.dispatch("mcp__bravo__repo_read", {"path": "app/db_secret.py"})
    assert value not in json.dumps(tools.events) and tools.events[-1]["outcome"] == "DENIED"


def test_demo_chat_db_secret_source_is_still_readable(context):
    content = (FIXTURES / "demo_chat_db_secret.py.txt").read_text()
    assert 'secret["password"]' in content  # references, not values
    tools = _snapshot_with(context, "app/db_secret.py", content)
    assert tools.dispatch("mcp__bravo__repo_read", {"path": "app/db_secret.py"})["content"] == content


@pytest.mark.parametrize("line", [
    'password = os.environ["DB_PASSWORD"]', 'password = os.getenv("DB_PASSWORD")',
    'self.password = password', 'return secret["username"], secret["password"]',
    'DB_SECRET_ID = "demo-chat/db"', 'api_key = settings.api_key',
    'url = "postgresql://chat-pooler-rw:5432/chat"', 'endpoint_url=os.environ.get("S3_ENDPOINT_URL")',
])
def test_credential_references_without_values_are_readable(context, line):
    tools = _snapshot_with(context, "app/db_secret.py", "import os\n" + line + "\n")
    assert line in tools.dispatch("mcp__bravo__repo_read", {"path": "app/db_secret.py"})["content"]


def test_inventory_summary_accepts_the_real_t16_producer_output(context):
    # Generated with services/inventory (PR #144 head 961fcd2) `bravo-inventory scan` on its
    # demo-chat fixture; only app.repo was replaced by a relative path.
    doc = json.loads((FIXTURES / "t16-inventory-demo-chat.json").read_text(encoding="utf-8"))
    assert doc["digest"].startswith("sha256:") and len(doc["digest"]) == 71
    summary = InventorySummary.from_document(doc)
    assert summary.digest == doc["digest"] and summary.app_id == "demo-chat"
    assert {i.role: i.grade for i in summary.items} == {
        "db": "config_swap", "iam": "adapter", "kms": "code_change", "object_store": "config_swap", "secrets": "code_change"}
    secrets = next(i for i in summary.items if i.role == "secrets")
    assert any(r.file == "app/db_secret.py" and r.kind == "sdk_call" for r in secrets.code_refs)
    bundle, snapshot, _, _ = context
    tools = Tools(bundle, snapshot, 1, inventory=summary)
    result = tools.dispatch("mcp__bravo__inventory_describe", {"role": "secrets"})
    dump = json.dumps(result)
    assert "snippet" not in dump and "deploy_refs" not in dump and "env_sources" not in dump
