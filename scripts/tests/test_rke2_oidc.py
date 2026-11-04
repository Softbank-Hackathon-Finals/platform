import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rke2_oidc_config import AUDIENCE, USERNAME_PREFIX, render
from rke2_sso import emit_credential, kubeconfig


class OIDCConfigTests(unittest.TestCase):
    def setUp(self):
        account = "0" * 12
        def fixture_arn(service, resource):
            return ":".join(("arn", "aws", service, "", account, resource))

        self.user = "abcdefab-1234-4567-8901-abcdefabcdef"
        self.config = {"account": account, "issuer": "https://test.tokens.sts.global.api.aws",
                       "role_arn": fixture_arn("iam", "role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_BravoDevelopmentAdministrator_0123456789abcdef"),
                       "store_arn": fixture_arn("identitystore", "identitystore/d-0000000000"),
                       "access_until": "2026-11-09T09:05:00+09:00", "members": {"member-03": self.user}}

    def test_bind_exact_human(self):
        auth, bindings = render(self.config)
        self.assertEqual(auth["jwt"][0]["issuer"]["audiences"], [AUDIENCE])
        self.assertEqual(bindings["items"][0]["subjects"], [
            {"kind": "User", "apiGroup": "rbac.authorization.k8s.io", "name": USERNAME_PREFIX + self.user}])

    def test_ambiguous_roster_and_other_account(self):
        for field, value in [("members", {"member-01": self.user, "member-03": self.user}),
                             ("account", "1" * 12), ("access_until", "2026-11-09T09:05:00")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                render(self.config | {field: value})


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.config = {"tls_name": "cluster.test", "server_ca_pem": "pinned test CA"}
        self.path = Path("/example/settings.json")
        self.cluster = kubeconfig(self.config, self.path, "bravo-personal", "https://127.0.0.1:12345")["clusters"][0]["cluster"]

    def test_no_token_for_another_endpoint_or_ca(self):
        for overrides in [{"server": "https://other.test:443"}, {"tls-server-name": "other.test"},
                          {"certificate-authority-data": ""}, {"insecure-skip-tls-verify": True},
                          {"proxy-url": "https://other.test"}]:
            info = json.dumps({"spec": {"cluster": self.cluster | overrides}})
            with self.subTest(overrides=overrides), patch.dict(os.environ, KUBERNETES_EXEC_INFO=info), patch("rke2_sso.aws_session") as session:
                with self.assertRaises(ValueError):
                    emit_credential(self.config, self.path, "bravo-personal")
                session.assert_not_called()

    def test_request_uses_single_audience_and_no_request_tags(self):
        info = json.dumps({"spec": {"cluster": self.cluster}})
        with patch.dict(os.environ, KUBERNETES_EXEC_INFO=info), patch("rke2_sso.aws_session") as session, redirect_stdout(io.StringIO()) as out:
            issue = session.return_value.client.return_value.get_web_identity_token
            issue.return_value = {"WebIdentityToken": "synthetic", "Expiration": datetime.now(timezone.utc)}
            emit_credential(self.config, self.path, "bravo-personal")
            issue.assert_called_once_with(Audience=[AUDIENCE], DurationSeconds=300, SigningAlgorithm="RS256")
            self.assertEqual(json.loads(out.getvalue())["status"]["token"], "synthetic")
