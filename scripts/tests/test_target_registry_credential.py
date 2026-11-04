"""Temporary registry auth must retain the narrowed identity and fixed endpoint."""

import importlib
import json
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
credential = importlib.import_module("target_registry_credential")
ACCOUNT = "123456789012"
REGISTRY = f"{ACCOUNT}.dkr.ecr.ap-northeast-2.amazonaws.com"
REPOSITORY = f"arn:aws:ecr:ap-northeast-2:{ACCOUNT}:repository/bravo/demo-chat"
CONTRACT = {"account_id": ACCOUNT, "region": "ap-northeast-2",
            "repository_arn": REPOSITORY, "repository_url": REGISTRY + "/bravo/demo-chat"}
NOW = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
SECRET = "synthetic-registry-authorization-never-print"


class Provider:
    def __init__(self):
        self.calls = []
        self.session = {"AccessKeyId": "synthetic-id", "SecretAccessKey": "synthetic-key",
                        "SessionToken": "synthetic-session", "Expiration": (NOW + timedelta(hours=1)).isoformat()}
        self.arn = None
        self.endpoint = "https://" + REGISTRY
        self.authorization_expiry = (NOW + timedelta(hours=12)).isoformat()
        self.narrowed_identity_matches = True

    def request(self, service, operation, parameters, credentials=None):
        self.calls.append((service, operation, parameters, credentials))
        if operation == "get-caller-identity":
            if credentials is None:
                return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/operator"}
            return {"Account": ACCOUNT, "Arn": self.arn if self.narrowed_identity_matches else "wrong"}
        if operation == "get-federation-token":
            self.arn = f"arn:aws:sts::{ACCOUNT}:federated-user/{parameters['Name']}"
            return {"Credentials": self.session, "FederatedUser": {"Arn": self.arn}}
        if operation == "get-authorization-token":
            return {"authorizationData": [{"proxyEndpoint": self.endpoint,
                                           "expiresAt": self.authorization_expiry,
                                           "authorizationToken": SECRET}]}
        raise AssertionError("unexpected provider operation")


class CredentialTest(unittest.TestCase):
    def test_narrowed_identity_and_distinct_expiries_are_retained(self):
        provider = Provider()
        material = credential.issue(CONTRACT, provider.request, NOW)
        self.assertEqual(material.refresh_before, provider.session["Expiration"])
        self.assertNotEqual(material.session_expires_at, material.registry_expires_at)
        self.assertNotIn(SECRET, repr(material))
        self.assertEqual(provider.calls[-1][3], provider.session)
        policy = json.loads(provider.calls[1][2]["Policy"])
        self.assertEqual(provider.calls[1][2]["DurationSeconds"], 3600)
        # Resource-policy grants cannot turn this credential into a writer or
        # authorize pulls from another repository: both have explicit denials.
        deny = [s for s in policy["Statement"] if s["Effect"] == "Deny"]
        self.assertNotIn("ecr:PutImage", deny[0]["NotAction"])
        self.assertEqual(deny[1]["NotResource"], REPOSITORY)
        self.assertIn("ecr:BatchGetImage", deny[1]["Action"])

    def test_wrong_account_region_or_repository_stops_before_credentials(self):
        for field, value in (("account_id", "wrong"), ("region", "us-east-1"),
                             ("repository_arn", REPOSITORY + "-other"),
                             ("repository_url", "attacker.invalid/demo-chat")):
            with self.subTest(field=field):
                provider = Provider()
                with self.assertRaises(credential.Failure):
                    credential.issue({**CONTRACT, field: value}, provider.request, NOW)
                self.assertFalse(provider.calls)

    def test_identity_mismatch_cannot_issue_registry_authorization(self):
        provider = Provider()
        provider.narrowed_identity_matches = False
        with self.assertRaisesRegex(credential.Failure, "narrowed_identity_invalid"):
            credential.issue(CONTRACT, provider.request, NOW)
        self.assertFalse(any(c[0] == "ecr" for c in provider.calls))

    def test_wrong_registry_and_expired_or_extended_authorization_are_rejected(self):
        for field, value in (("endpoint", "https://attacker.invalid"),
                             ("authorization_expiry", NOW.isoformat()),
                             ("authorization_expiry", (NOW + timedelta(days=1)).isoformat())):
            with self.subTest(field=field, value=value):
                provider = Provider()
                setattr(provider, field, value)
                with self.assertRaises(credential.Failure):
                    credential.issue(CONTRACT, provider.request, NOW)

    def test_expired_session_stops_before_further_use(self):
        provider = Provider()
        provider.session["Expiration"] = NOW.isoformat()
        with self.assertRaisesRegex(credential.Failure, "session_expiry_invalid"):
            credential.issue(CONTRACT, provider.request, NOW)
        self.assertEqual(len(provider.calls), 2)

    def test_transport_uses_fixed_tls_endpoint_and_no_secret_arguments(self):
        provider = Provider()
        with patch.dict(os.environ, {"AWS_PROFILE": "ambient", "AWS_ENDPOINT_URL": "https://wrong.invalid"}), \
                patch.object(credential.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="{}")) as run:
            credential.aws("ecr", "get-authorization-token", {}, provider.session)
        command = run.call_args.args[0]
        env = run.call_args.kwargs["env"]
        self.assertIn("https://ecr.ap-northeast-2.amazonaws.com", command)
        self.assertNotIn("AWS_PROFILE", env)
        self.assertNotIn("AWS_ENDPOINT_URL", env)
        for value in provider.session.values():
            self.assertNotIn(value, command)
        self.assertEqual(env["AWS_SESSION_TOKEN"], provider.session["SessionToken"])

    def test_provider_error_text_is_never_forwarded(self):
        response = SimpleNamespace(returncode=1, stdout=SECRET, stderr=SECRET)
        with patch.object(credential.subprocess, "run", return_value=response), \
                self.assertRaisesRegex(credential.Failure, "^provider_request_failed$"):
            credential.aws("sts", "get-caller-identity", {})


if __name__ == "__main__":
    unittest.main()
