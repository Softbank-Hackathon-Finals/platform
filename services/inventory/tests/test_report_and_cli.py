import json
from datetime import UTC, datetime

import jsonschema
import pytest

from bravo_inventory import build_inventory, validate_inventory
from bravo_inventory.cli import main
from bravo_inventory.deploy_trace import FileAdapter


def _build(repo, exports, profiles):
    return build_inventory(
        app_id="demo-chat", repo=repo, app_path="app",
        source_env={"env_id": "aws-seoul", "env_kind": "aws", "namespace": "chat"},
        adapter=FileAdapter(exports), profiles=profiles,
        exports={"onprem-demo": exports.parent / "onprem"},
        now=datetime(2026, 11, 4, 0, 0, tzinfo=UTC),
    )


def test_document_validates_and_summarises(repo, exports, profiles):
    doc = _build(repo, exports, profiles)
    validate_inventory(doc)
    assert doc["summary"]["deploy_only"] == 1  # kms
    assert doc["summary"]["grade_counts"]["code_change"] >= 1
    assert doc["inputs"]["api_calls"] == sorted(doc["inputs"]["api_calls"])


def test_candidates_fit_uses_capabilities(repo, exports, profiles):
    doc = _build(repo, exports, profiles)
    by_id = {c["env_id"]: c for c in doc["candidates"]}
    assert by_id["onprem-demo"]["verdict"] == "needs_work"  # code_change items remain
    assert by_id["onprem-demo"]["capabilities"]["s3_compatible"]["source"].startswith("observed")
    assert by_id["onprem-minimal"]["verdict"] == "blocked"
    assert any("s3_compatible" in m for m in by_id["onprem-minimal"]["missing_capabilities"])


def test_schema_rejects_agent_grade_without_rationale(repo, exports, profiles):
    doc = _build(repo, exports, profiles)
    doc["items"][0]["grade_by"] = "agent"
    doc["items"][0]["rationale"] = "short"
    with pytest.raises(jsonschema.ValidationError):
        validate_inventory(doc)


def test_cli_writes_once(repo, exports, profiles, tmp_path, capsys):
    out = tmp_path / "inventory.json"
    rc = main(["scan", "--app-id", "demo-chat", "--repo", str(repo), "--app-path", "app", "--source-env", "aws-seoul",
               "--namespace", "chat", "--exports", str(exports), "--profile", str(profiles[0]), "--output", str(out)])
    assert rc == 0 and json.loads(out.read_text())["schema_version"] == "1.0"
    assert "items=" in capsys.readouterr().out
    assert main(["scan", "--app-id", "demo-chat", "--repo", str(repo), "--app-path", "app", "--source-env", "aws-seoul",
                 "--namespace", "chat", "--profile", str(profiles[0]), "--output", str(out)]) == 2


def test_final_document_contains_no_injected_credentials(repo, exports, profiles, tmp_path):
    # End-to-end boundary: synthetic credentials placed in code, config and Deployment env must
    # be absent from the whole inventory document, not just from individual findings.
    import shutil

    from bravo_inventory.deploy_trace import FileAdapter
    from bravo_inventory.report import build_inventory

    canary = "CANARY-9b7d2e-SECRET"
    work = tmp_path / "repo"
    shutil.copytree(repo, work)
    (work / "app" / "leaky.py").write_text(
        "import boto3\n"
        f'c = boto3.client("s3", endpoint_url="https://u:{canary}@store.example.invalid/?token={canary}")\n'
        f'DATABASE_URL = "postgresql://chat:{canary}@db:5432/chat"\n', encoding="utf-8")
    (work / "compose.yaml").write_text((work / "compose.yaml").read_text(encoding="utf-8") + f"      S3_ENDPOINT_URL: http://ak:{canary}@minio:9000\n      AWS_SECRET_ACCESS_KEY: {canary}\n", encoding="utf-8")
    ex = tmp_path / "exports"
    shutil.copytree(exports, ex)
    dep = json.loads((ex / "deployments.json").read_text(encoding="utf-8"))
    dep["items"][0]["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "DATABASE_URL", "value": f"postgresql://chat:{canary}@chat-pooler-rw:5432/chat"})
    (ex / "deployments.json").write_text(json.dumps(dep), encoding="utf-8")
    doc = build_inventory(app_id="demo-chat", repo=work, app_path="app", source_env={"env_id": "aws-seoul", "env_kind": "aws", "namespace": "chat"},
                          adapter=FileAdapter(ex), profiles=profiles, source_sha="1" * 40)
    assert canary not in json.dumps(doc, ensure_ascii=False)
    assert any(w["endpoints"].get("DATABASE_URL") == "postgresql://chat-pooler-rw:5432" for w in doc["deploy"]["workloads"])
