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
    assert doc["summary"]["deploy_only"] == 2  # kms + the legacy-audit bucket no setting refers to
    assert doc["summary"]["grade_counts"]["code_change"] >= 1
    assert doc["inputs"]["api_calls"] == sorted(doc["inputs"]["api_calls"])


def test_repo_digest_covers_every_scanned_file_not_only_app_path(repo, tmp_path):
    # Real shakedown 2026-11-06: compose.yaml (repo root) is scanned for config but was outside
    # repo_digest, so a config change left the "what was read" digest unchanged.
    import shutil

    from bravo_inventory.report import repo_digest

    work = tmp_path / "repo"
    shutil.copytree(repo, work)
    before = repo_digest(work, "app")
    (work / "compose.yaml").write_text((work / "compose.yaml").read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    assert repo_digest(work, "app") != before


def test_document_does_not_depend_on_checkout_location(repo, exports, profiles, tmp_path):
    # Real shakedown: app.repo held the absolute checkout path (a home directory), so the document
    # digest differed per checkout and the path leaked into a document the console and model read.
    import shutil

    a, b = tmp_path / "a" / "demo-chat", tmp_path / "b" / "demo-chat"
    shutil.copytree(repo, a)
    shutil.copytree(repo, b)
    da, db = _build(a, exports, profiles), _build(b, exports, profiles)
    assert da["digest"] == db["digest"] and da["inputs"]["repo_digest"] == db["inputs"]["repo_digest"]
    assert da["app"]["repo"] == "demo-chat" and str(tmp_path) not in json.dumps(da, ensure_ascii=False)
    ref = build_inventory(app_id="demo-chat", repo=a, repo_ref="Softbank-Hackathon-Finals/demo-chat", app_path="app",
                          source_env={"env_id": "aws-seoul", "env_kind": "aws", "namespace": "chat"}, adapter=None, profiles=profiles)
    assert ref["app"]["repo"] == "Softbank-Hackathon-Finals/demo-chat"


def test_missing_capability_is_listed_once_per_role(repo, exports, profiles):
    # Real shakedown: a role with a split-out deploy-only item listed the same missing capability twice.
    doc = _build(repo, exports, profiles)
    minimal = {c["env_id"]: c for c in doc["candidates"]}["onprem-minimal"]
    assert sum(1 for i in doc["items"] if i["role"] == "object_store") == 2
    assert len(minimal["missing_capabilities"]) == len(set(minimal["missing_capabilities"]))


def test_statefulset_workloads_are_observed_as_capabilities(tmp_path):
    # Real shakedown: the target runs OpenBao as a StatefulSet; only Deployments were read, so
    # secret_store stayed "declared" although it was observable.
    from bravo_inventory.capabilities import observe

    (tmp_path / "statefulsets.json").write_text(json.dumps({"kind": "StatefulSetList", "items": [{"metadata": {"name": "target-openbao", "namespace": "bravo-secrets"}}]}), encoding="utf-8")
    assert observe(tmp_path)["secret_store"] == {"value": "openbao", "source": "observed: StatefulSet/target-openbao"}


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
        f'DATABASE_URL = "postgresql://chat:{canary}@db:5432/chat"\n'
        f'A = "https://store.example.invalid/?client_secret={canary}"\n'
        f'B = "https://api.ap-northeast-2.amazonaws.com/x?%61ccess_token={canary}"\n'
        f'C = boto3.client("s3", endpoint_url="https://store.example.invalid/?client_secret=\'{canary}\'")\n'
        f'D = boto3.client("s3", endpoint_url="https://store.example.invalid/#client_secret=){canary}")\n'
        f'E=boto3.client("s3"); F="https://store.example.invalid/\\x3fclient_secret={canary}"\n', encoding="utf-8")
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
