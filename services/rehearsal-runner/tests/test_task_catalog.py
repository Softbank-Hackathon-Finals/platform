import json
import os
from types import SimpleNamespace

import pytest

from rehearsal_runner.admission import logical_task_digest
from rehearsal_runner.bucket_task import BucketPolicyTask
from rehearsal_runner.secret_task import CredentialMigrationTask
from rehearsal_runner.task_catalog import CatalogUnavailable, TaskCatalog, main

KEY = b"c" * 32
POLICY = '{"Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"x"}]}'
PLAN = {"plan_id": "plan-1", "tenant_id": "tenant-1", "spec_json": json.dumps({"plan_digest": "a" * 64})}


def register(catalog, **changes):
    values = dict(plan_id="plan-1", tenant_id="tenant-1", plan_digest="a" * 64, task_id="bucket",
                  kind="bucket-policy", source_sha="1" * 40, source_policy=POLICY,
                  target_bucket="rehearsal-attachments")
    return catalog.register(**(values | changes))


def test_registered_tasks_are_read_for_the_matching_plan_and_admission(tmp_path):
    catalog = TaskCatalog(tmp_path, KEY)
    register(catalog)
    register(catalog, task_id="credentials", kind="credential-migration", source_policy=None, target_bucket=None)
    bucket = catalog.for_plan(None, PLAN, "bucket")
    assert type(bucket) is BucketPolicyTask and bucket.target_bucket == "rehearsal-attachments"
    assert type(catalog.for_plan(None, PLAN, "credentials")) is CredentialMigrationTask
    admission = SimpleNamespace(plan_id="plan-1", tenant_id="tenant-1", task_id="bucket")
    assert logical_task_digest(catalog.for_admission(admission)) == logical_task_digest(bucket)


def test_registration_is_write_once(tmp_path):
    catalog = TaskCatalog(tmp_path, KEY)
    register(catalog)
    with pytest.raises(CatalogUnavailable, match="already registered"):
        register(catalog, target_bucket="other-bucket")


@pytest.mark.parametrize("plan", [
    PLAN | {"spec_json": json.dumps({"plan_digest": "b" * 64})},
    PLAN | {"tenant_id": "tenant-2"},
])
def test_changed_plan_or_other_tenant_does_not_reuse_the_task(tmp_path, plan):
    catalog = TaskCatalog(tmp_path, KEY)
    register(catalog)
    with pytest.raises(CatalogUnavailable):
        catalog.for_plan(None, plan, "bucket")


@pytest.mark.parametrize("change", ["tamper", "key", "missing"])
def test_altered_unknown_or_foreign_registration_is_unavailable(tmp_path, change):
    catalog = TaskCatalog(tmp_path, KEY)
    register(catalog)
    if change == "tamper":
        path = tmp_path / "plan-1" / "bucket.json"
        raw = json.loads(path.read_text())
        raw["record"]["target_bucket"] = "demo-chat-attachments"
        path.write_text(json.dumps(raw))
    elif change == "key":
        catalog = TaskCatalog(tmp_path, b"o" * 32)
    task_id = "other" if change == "missing" else "bucket"
    with pytest.raises(CatalogUnavailable):
        catalog.for_plan(None, PLAN, task_id)


@pytest.mark.parametrize("changes", [
    {"kind": "unknown"},
    {"target_bucket": "Bad_Bucket"},
    {"source_sha": "short"},
    {"plan_digest": "x"},
    {"kind": "credential-migration"},  # a credential task takes no policy or bucket
])
def test_invalid_registration_is_refused(tmp_path, changes):
    with pytest.raises(ValueError):
        register(TaskCatalog(tmp_path, KEY), **changes)


def test_operator_cli_registers_with_a_private_key_file(tmp_path, capsys):
    key_file, policy = tmp_path / "key", tmp_path / "policy.json"
    key_file.write_bytes(KEY)
    os.chmod(key_file, 0o600)
    policy.write_text(POLICY)
    args = ["--root", str(tmp_path / "catalog"), "--key-file", str(key_file), "--plan-id", "plan-1",
            "--tenant-id", "tenant-1", "--plan-digest", "a" * 64, "--task-id", "bucket", "--source-sha", "1" * 40,
            "--kind", "bucket-policy", "--source-policy-file", str(policy), "--target-bucket", "rehearsal-attachments"]
    assert main(args) == 0
    assert POLICY not in capsys.readouterr().out
    assert main(args) == 1
    os.chmod(key_file, 0o644)
    with pytest.raises(SystemExit):
        main(args)


def test_largest_allowed_policy_registers_and_reads_back(tmp_path):
    catalog = TaskCatalog(tmp_path, KEY)
    policy = '{"Statement":[]}' + "\n" * 32752
    register(catalog, source_policy=policy)
    assert catalog.for_plan(None, PLAN, "bucket").source_policy == policy


def test_oversized_registration_is_refused_before_the_file_exists(tmp_path, monkeypatch):
    from rehearsal_runner import task_catalog

    monkeypatch.setattr(task_catalog, "MAX_REGISTRATION_BYTES", 256)
    with pytest.raises(ValueError, match="too large"):
        register(TaskCatalog(tmp_path, KEY))
    assert not (tmp_path / "plan-1" / "bucket.json").exists()
