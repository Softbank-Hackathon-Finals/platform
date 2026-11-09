"""Independent synthetic SDK refresh counterexample; no real cache or network."""
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
import botocore.session
import pytest
from botocore import UNSIGNED
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.credentials import DeferredRefreshableCredentials

from bravo_platform_events.registration_sso import make_registration_token


@pytest.mark.parametrize("logger_name", ["botocore.parsers", "botocore.endpoint"])
def test_sdk_debug_cannot_publish_sso_credentials(monkeypatch, caplog, logger_name):
    client = botocore.session.get_session().create_client(
        "sso", region_name="ap-northeast-2", config=Config(signature_version=UNSIGNED),
        aws_access_key_id="synthetic-unused", aws_secret_access_key="synthetic-unused",
    )
    access, secret, session, bearer = ("ASIA" + "A" * 16, "review-secret-key-marker",
                                       "review-session-token-marker", "review-sso-bearer-marker")
    expiry = datetime.now(UTC) + timedelta(hours=1)
    payload = json.dumps({"roleCredentials": {"accessKeyId": access,
        "secretAccessKey": secret, "sessionToken": session,
        "expiration": int(expiry.timestamp() * 1000)}}).encode()

    class Raw:
        def stream(self, *args, **kwargs):
            yield payload

    def send(request):
        return AWSResponse(request.url, 200, {"content-type": "application/json"}, Raw())
    monkeypatch.setattr(client._endpoint.http_session, "send", send)

    def refresh():
        value = client.get_role_credentials(roleName="Fixture", accountId="111122223333",
                                             accessToken=bearer)["roleCredentials"]
        return dict(access_key=value["accessKeyId"], secret_key=value["secretAccessKey"],
                    token=value["sessionToken"], expiry_time=expiry.isoformat())
    creds = DeferredRefreshableCredentials(refresh_using=refresh, method="sso")
    monkeypatch.setattr(boto3, "Session", lambda **kwargs: SimpleNamespace(get_credentials=lambda: creds))
    caplog.set_level(logging.WARNING)
    caplog.set_level(logging.WARNING, logger="botocore.auth")
    caplog.set_level(logging.DEBUG, logger=logger_name)
    try:
        make_registration_token(profile_name="synthetic", region="ap-northeast-2", audience="review")
    except RuntimeError as error:
        assert str(error) == "registration_sso_proof_unavailable"
    finally:
        client.close()
    exposed = [name for name, value in [("access_key", access), ("secret_key", secret),
               ("session_token", session), ("sso_bearer", bearer)] if value in caplog.text]
    assert not exposed, f"{logger_name} exposed synthetic {exposed}"
