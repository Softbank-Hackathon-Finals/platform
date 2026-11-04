import pytest

from rehearsal_runner.secret_task import prepare_credential_migration


def test_instructions_reference_expected_source_and_projected_file():
    task = prepare_credential_migration("run-1", "a" * 40)
    data = task.as_model_input()
    assert data["source_sha"] == "a" * 40
    assert data["source_paths"] == ["app/db_secret.py", "app/config.py", "app/main.py"]
    assert data["target_credential_file"] == "/run/bravo/database/credentials.json"
    assert data["required_keys"] == ["username", "password"]
    assert "verdict" not in data and "approval" not in data
    assert data["requirements"] and "300-second" in data["requirements"][1]


@pytest.mark.parametrize("run_id,sha", [("", "a" * 40), ("../other", "a" * 40),
                                         ("run-1", "A" * 40), ("run-1", "a" * 39)])
def test_invalid_execution_identity_is_rejected(run_id, sha):
    with pytest.raises(ValueError):
        prepare_credential_migration(run_id, sha)
