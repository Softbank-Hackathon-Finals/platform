"""Local simulation: no Kubernetes, OpenBao, ESO or DB service is contacted."""

import json

import pytest

from rehearsal_runner.secret_task import prepare_credential_migration


class FakeSecretDelivery:
    def __init__(self, value):
        self.value = value

    def read(self, identity):
        if identity != "application":
            raise PermissionError("access denied")
        return self.value


class FakeDatabase:
    def __init__(self, credentials):
        self.credentials = credentials

    def connect(self, credentials):
        return credentials == self.credentials


def simulate_app_connect(delivery, database):
    try:
        raw = delivery.read("application")
        credentials = json.loads(raw)
        if not isinstance(credentials, dict) or any(
            not isinstance(credentials.get(key), str) or not credentials[key]
            for key in ("username", "password")
        ):
            return "unknown"
        return "connected" if database.connect((credentials["username"], credentials["password"])) else "failed"
    except (PermissionError, TypeError, ValueError):
        return "unknown"


def test_mock_delivery_and_access_denial_do_not_assert_real_rbac():
    task = prepare_credential_migration("run-1", "a" * 40)
    delivery = FakeSecretDelivery(json.dumps({"username": "fixture-user", "password": "fixture-value"}))
    db = FakeDatabase(("fixture-user", "fixture-value"))
    assert task.as_model_input()["target_credential_file"].endswith("/credentials.json")
    assert simulate_app_connect(delivery, db) == "connected"
    with pytest.raises(PermissionError):
        delivery.read("another-service-account")


@pytest.mark.parametrize("value", ["not-json", "{}", '{"username":"fixture-user"}', None])
def test_missing_or_invalid_mock_credentials_are_not_success(value):
    assert simulate_app_connect(FakeSecretDelivery(value), FakeDatabase(("fixture-user", "fixture-value"))) == "unknown"


def test_mock_database_rejects_wrong_credentials():
    delivery = FakeSecretDelivery(json.dumps({"username": "fixture-user", "password": "different"}))
    assert simulate_app_connect(delivery, FakeDatabase(("fixture-user", "fixture-value"))) == "failed"
