from dataclasses import replace

import pytest

from rehearsal_runner.admission import logical_task_digest
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.secret_task import prepare_credential_migration


def test_bucket_task_identity_ignores_run_id_but_binds_source_and_target():
    first = prepare_bucket_policy_task("run-1", '{"Statement":[]}', "fixture-bucket", source_sha="a" * 40)
    second = prepare_bucket_policy_task("run-2", '{"Statement":[]}', "fixture-bucket", source_sha="a" * 40)
    changed = prepare_bucket_policy_task("run-2", '{"Statement":[]}', "other-bucket", source_sha="a" * 40)
    assert logical_task_digest(first) == logical_task_digest(second)
    assert logical_task_digest(first) != logical_task_digest(changed)
    with pytest.raises(ValueError):
        logical_task_digest(replace(first, source_sha256="f" * 64))


def test_credential_task_identity_is_stable_across_runs_and_changes_with_revision():
    first = prepare_credential_migration("run-1", "a" * 40)
    second = prepare_credential_migration("run-2", "a" * 40)
    changed = prepare_credential_migration("run-1", "b" * 40)
    assert logical_task_digest(first) == logical_task_digest(second)
    assert logical_task_digest(first) != logical_task_digest(changed)


def test_unknown_task_cannot_be_admitted():
    with pytest.raises(ValueError):
        logical_task_digest(object())
