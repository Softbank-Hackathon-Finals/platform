import json

import pytest

from bravo_agent import subscription_server as server


def test_broker_uses_web_identity_in_memory_and_reports_rotation(monkeypatch, tmp_path):
    calls = []
    credentials = {"AccessKeyId": "test-access", "SecretAccessKey": "test-private", "SessionToken": "test-session"}

    class STS:
        def assume_role_with_web_identity(self, **kwargs):
            calls.append(kwargs)
            return {"Credentials": credentials}

    def client(name, **kwargs):
        calls.append({"service": name, **kwargs})
        return STS() if name == "sts" else object()

    class Store:
        def __init__(self, *args, **kwargs):
            self.count = 0

        def read(self):
            self.count += 1
            return {"state": "ACTIVE", "client_id": "same-client", "subject": "same-subject",
                    "access_token": str(self.count), "refresh_token": str(self.count),
                    "expires_at": self.count}, str(self.count)

    def worker(store, state, args):
        assert args.snapshot == tmp_path / "examples/source"
        args.output.write_text(json.dumps({"state": "PROPOSAL_READY"}))
        return 0

    monkeypatch.setattr(server.boto3, "client", client)
    monkeypatch.setattr(server, "SubscriptionStore", Store)
    monkeypatch.setattr(server, "run_worker", worker)
    result = server.execute({"region": "ap-northeast-2", "broker_role_arn": "test-role",
                             "secret_arn": "test-secret", "runtime_host_id": "test-host",
                             "worker": str(tmp_path), "state_dir": str(tmp_path / "state"),
                             "model": "test-model", "source_sha": "a" * 40}, "test-web-identity")
    assert calls[1] == {"RoleArn": "test-role", "RoleSessionName": "bravo-subscription-vm",
                        "WebIdentityToken": "test-web-identity", "DurationSeconds": 900}
    assert calls[2]["aws_session_token"] == credentials["SessionToken"]
    assert all(result[name] for name in ("credential_rotated", "access_rotated", "refresh_rotated",
                                        "registration_reused", "subject_reused", "expires_at_increased"))
    assert "test-web-identity" not in json.dumps(result)
    assert "test-private" not in json.dumps(result)


def test_rejected_identity_never_reads_secret_or_starts_model(monkeypatch):
    class STS:
        def assume_role_with_web_identity(self, **kwargs):
            raise ValueError("identity denied")

    monkeypatch.setattr(server.boto3, "client", lambda name, **kwargs: STS())
    monkeypatch.setattr(server, "run_worker", lambda *args: pytest.fail("must not start model"))
    with pytest.raises(ValueError, match="identity denied"):
        server.execute({"region": "ap-northeast-2", "broker_role_arn": "test-role"}, "invalid")
