# /// script
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Rendered initial-proposal Job boundary, and that the operator's Job fits it."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def render(values):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "values.json"
        path.write_text(json.dumps(values))
        return subprocess.run(["helm", "template", "bravo-gitops", str(ROOT / "infra/gitops/argocd"),
            "--namespace", "bravo-gitops", "--kube-version", "1.35.9", "-f", str(path)],
            check=False, capture_output=True, text=True, timeout=30)


def owned(stdout):
    names = {"bravo-initial", "initial-proposal", "subscription-broker-ca", "platform-initial-proposal-pods"}
    return [d for d in yaml.safe_load_all(stdout) if d and d["metadata"]["name"] in names]


class InitialProposalChart(unittest.TestCase):
    def test_disabled_by_default(self):
        result = render({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(owned(result.stdout), [])

    def test_namespace_account_ca_and_pod_policy(self):
        result = render({"initialProposal": {"enabled": True, "brokerCaPem": CA}})
        self.assertEqual(result.returncode, 0, result.stderr)
        docs = {d["kind"]: d for d in owned(result.stdout)}
        self.assertCountEqual(docs, ["Namespace", "ServiceAccount", "ConfigMap",
                                     "ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"])
        self.assertEqual(docs["Namespace"]["metadata"]["labels"]["pod-security.kubernetes.io/warn"], "restricted")
        self.assertIs(docs["ServiceAccount"]["automountServiceAccountToken"], False)
        self.assertEqual(docs["ConfigMap"]["data"], {"ca.pem": CA})
        self.assertEqual(docs["ValidatingAdmissionPolicyBinding"]["spec"]["validationActions"], ["Deny"])
        self.assertNotIn("RoleBinding", docs)

    def test_private_key_or_missing_ca_is_refused(self):
        for value in ("", "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n"):
            self.assertNotEqual(render({"initialProposal": {"enabled": True, "brokerCaPem": value}}).returncode, 0)

    def test_operator_job_matches_policy_fields(self):
        # The operator's Pod spec must use exactly the fields the policy admits.
        source = (ROOT / "services/platform-events/src/bravo_platform_events/initial_proposal_job.py").read_text()
        for field in ('"automountServiceAccountToken": False', '"restartPolicy": "Never"', '"readOnlyRootFilesystem": True',
                      '"allowPrivilegeEscalation": False', '"drop": ["ALL"]', '"runAsNonRoot": True',
                      '"expirationSeconds": 600', 'AUDIENCE = "bravo-subscription-broker"',
                      'NAMESPACE = "bravo-initial"', 'SERVICE_ACCOUNT = "initial-proposal"'):
            self.assertIn(field, source)


if __name__ == "__main__":
    unittest.main()
