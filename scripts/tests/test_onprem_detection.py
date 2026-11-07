"""T33 on-prem detection bundle: audit-only Kyverno, Falco over modern eBPF, and a read-only outbound agent."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "infra/envs/onprem-detection"
MAPPINGS = json.loads((ROOT / "docs/security/detection/mappings.json").read_text())


class OnpremDetectionBundle(unittest.TestCase):
    def test_kyverno_only_audits_and_never_blocks_admission(self):
        values = (BUNDLE / "kyverno-values.yaml").read_text()
        self.assertIn("f6fe7184544d5e2ee33a77808c09e67aa603ce6560cc197038fde31d727c861e", values)
        for block in ("forceFailurePolicyIgnore:\n    enabled: true", "generateValidatingAdmissionPolicy:\n    enabled: false",
                      "openreports:\n  enabled: false", "cleanupController:\n  enabled: false"):
            self.assertIn(block, values)
        excluded = re.search(r"values: \[([^\]]*)\]", values).group(1).replace(" ", "").split(",")
        self.assertTrue({"kube-system", "tailscale-system", "cnpg-system", "bravo-secrets", "falco"} <= set(excluded))

    def test_policies_are_audit_and_mapped_to_a_category(self):
        mapped = {rule["equals"] for rule in MAPPINGS["category"]["kyverno"] if rule["field"] == "policy"}
        policies = [json.loads(p.read_text()) for p in sorted((BUNDLE / "policies").glob("*.json"))]
        self.assertTrue(policies)
        for policy in policies:
            self.assertEqual(policy["kind"], "ClusterPolicy")
            self.assertIn(policy["metadata"]["name"], mapped)
            self.assertEqual(policy["spec"]["validationFailureAction"], "Audit")
            self.assertIs(policy["spec"]["background"], True)
            self.assertIn(policy["metadata"]["annotations"]["policies.kyverno.io/severity"], {"low", "medium", "high", "critical"})

    def test_falco_sends_through_a_secret_never_values(self):
        values = (BUNDLE / "falco-values.yaml").read_text()
        self.assertIn("b8a1aa984c3d513ab1639be760e6a07a9eb203d4da77a45a1512d382b6d3687d", values)
        self.assertIn("kind: modern_ebpf", values)
        self.assertIn("existingSecret: falcosidekick-bravo", values)
        self.assertNotRegex(values, r"(?i)bearer|address:|customheaders|token:")

    def test_agent_reads_reports_only_and_runs_locked_down(self):
        items = {(o["kind"], o["metadata"]["name"]): o for o in json.loads((BUNDLE / "agent.json").read_text())["items"]}
        rules = items[("ClusterRole", "bravo-findings-agent")]["rules"]
        self.assertEqual(rules, [{"apiGroups": ["wgpolicyk8s.io"], "resources": ["policyreports", "clusterpolicyreports"],
                                  "verbs": ["get", "list"]}])
        falco = items[("Role", "bravo-findings-agent")]
        self.assertEqual((falco["metadata"]["namespace"], falco["rules"]),
                         ("falco", [{"apiGroups": ["apps"], "resources": ["daemonsets"], "resourceNames": ["falco"], "verbs": ["get"]}]))
        pod = items[("CronJob", "bravo-findings-agent")]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        container, = pod["containers"]
        self.assertRegex(container["image"], r"@sha256:[0-9a-f]{64}$")
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(container["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                        "capabilities": {"drop": ["ALL"]}})
        self.assertFalse([e for e in container["env"] if "TOKEN" in e["name"] and e["name"] != "BRAVO_FINDINGS_TOKEN_FILE"])
        self.assertEqual(pod["volumes"][0]["secret"]["defaultMode"], 0o400)


if __name__ == "__main__":
    unittest.main()
