import hashlib

import pytest

from rehearsal_runner.bucket_task import prepare_bucket_policy_task

SOURCE = '{"Version":"2012-10-17","Statement":[{"Effect":"Deny","Action":"s3:GetObject","Principal":"*","Resource":"arn:aws:s3:::example/*"}]}'


def test_task_captures_exact_source_and_scope_without_applying_it():
    task = prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha="a" * 40)
    data = task.as_model_input()
    assert data["source_sha"] == "a" * 40
    assert data["source_policy"] == SOURCE
    assert data["source_sha256"] == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert data["target_bucket"] == "rehearsal-test"
    assert data["target_service"] == "seaweedfs"
    assert "verdict" not in data and "approval" not in data


@pytest.mark.parametrize("source", ['{"Statement":[],"Statement":[]}', '[]', '{}', '{bad'])
def test_invalid_or_ambiguous_source_is_rejected(source):
    with pytest.raises(ValueError):
        prepare_bucket_policy_task("run-1", source, "rehearsal-test", source_sha="a" * 40)


@pytest.mark.parametrize("bucket", ["*", "ab", "../production", "UPPER", "a" * 64])
def test_invalid_bucket_is_rejected(bucket):
    with pytest.raises(ValueError):
        prepare_bucket_policy_task("run-1", SOURCE, bucket, source_sha="a" * 40)


def test_input_size_is_bounded():
    with pytest.raises(ValueError):
        prepare_bucket_policy_task("run-1", '{"Statement":[],"padding":"' + 'x' * 32768 + '"}', "rehearsal-test", source_sha="a" * 40)


@pytest.mark.parametrize("revision", ["", "a" * 39, "A" * 40])
def test_invalid_revision_is_rejected(revision):
    with pytest.raises(ValueError):
        prepare_bucket_policy_task("run-1", SOURCE, "rehearsal-test", source_sha=revision)
