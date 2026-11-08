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
            # Background scans only: no admission webhook on workload Pods during a cutover, and no
            # spec-level validationFailureAction (deprecated in 1.19); each rule audits.
            self.assertIs(policy["spec"]["admission"], False)
            self.assertIs(policy["spec"]["background"], True)
            self.assertNotIn("validationFailureAction", policy["spec"])
            self.assertEqual({r["validate"]["failureAction"] for r in policy["spec"]["rules"]}, {"Audit"})
            # One count per Pod, and system/platform Pods are not tenant violations.
            self.assertEqual(policy["metadata"]["annotations"]["pod-policies.kyverno.io/autogen-controllers"], "none")
            for rule in policy["spec"]["rules"]:
                excluded = {n for e in rule["exclude"]["any"] for n in e["resources"]["namespaces"]}
                self.assertTrue({"kube-system", "tailscale-system", "falco", "kyverno", "bravo-security"} <= excluded)
            self.assertIn(policy["metadata"]["annotations"]["policies.kyverno.io/severity"], {"low", "medium", "high", "critical"})

    def test_falco_sends_through_a_secret_never_values(self):
        values = (BUNDLE / "falco-values.yaml").read_text()
        self.assertIn("b8a1aa984c3d513ab1639be760e6a07a9eb203d4da77a45a1512d382b6d3687d", values)
        self.assertIn("kind: modern_ebpf", values)
        self.assertIn("existingSecret: falcosidekick-bravo", values)
        self.assertNotRegex(values, r"(?i)bearer|address:|customheaders|token:")
        # Rules are fetched once at install, never followed at runtime; alerts never land in Pod logs.
        self.assertIn("falcoctl:\n  artifact:\n    follow:\n      enabled: false", values)
        self.assertIn("stdout_output:\n    enabled: false", values)
        # The platform's private CA comes from a ConfigMap (a public certificate), never a key.
        self.assertIn("cacertfile: /etc/bravo-findings-ca/ca.crt", values)
        self.assertIn("configMap: {name: bravo-findings-ca}", values)

    def test_every_installed_image_is_pinned_by_digest_and_rules_by_exact_version(self):
        # The charts assemble registry/repository:tag only, so the digest rides on the tag.
        pinned = r'tag: "[0-9A-Za-z.\-]+@sha256:[0-9a-f]{64}"'
        kyverno = (BUNDLE / "kyverno-values.yaml").read_text()
        falco = (BUNDLE / "falco-values.yaml").read_text()
        self.assertEqual(len(re.findall(pinned, kyverno)), 7)  # 4 controllers + pre container + upgrade/delete hooks + test
        self.assertEqual(len(re.findall(pinned, falco)), 3)  # falco, falcoctl, falcosidekick
        self.assertNotRegex(kyverno + falco, r'tag: "?[^"@\n]+"?\n')  # no tag without a digest
        self.assertEqual(re.findall(r"refs: \[(falco-rules:[^\]]+)\]", falco), ["falco-rules:5.2.0"] * 2)

    def test_agent_reads_reports_only_and_runs_locked_down(self):
        items = {(o["kind"], o["metadata"]["name"]): o for o in json.loads((BUNDLE / "agent.json").read_text())["items"]}
        rules = items[("ClusterRole", "bravo-findings-agent")]["rules"]
        self.assertEqual(rules, [{"apiGroups": ["wgpolicyk8s.io"], "resources": ["policyreports", "clusterpolicyreports"],
                                  "verbs": ["get", "list"]}])
        roles = {o["metadata"]["namespace"]: o["rules"] for o in json.loads((BUNDLE / "agent.json").read_text())["items"]
                 if o["kind"] == "Role"}
        self.assertEqual(roles, {
            "falco": [{"apiGroups": ["apps"], "resources": ["daemonsets"], "resourceNames": ["falco"], "verbs": ["get"]},
                      {"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["falco-falcosidekick"],
                       "verbs": ["get"]}],
            "kyverno": [{"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["kyverno-reports-controller"],
                         "verbs": ["get"]}]})
        pod = items[("CronJob", "bravo-findings-agent")]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        labels = items[("CronJob", "bravo-findings-agent")]["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(labels, {"app.kubernetes.io/name": "bravo-findings-agent",
                                  "bravo.dev/findings-client": "agent"})
        container, = pod["containers"]
        self.assertRegex(container["image"], r"@sha256:[0-9a-f]{64}$")
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(container["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                        "capabilities": {"drop": ["ALL"]}})
        self.assertFalse([e for e in container["env"] if "TOKEN" in e["name"] and e["name"] != "BRAVO_FINDINGS_TOKEN_FILE"])
        # uid 10001 reads the token through its group: root-owned 0400 would be unreadable.
        self.assertEqual((pod["securityContext"]["fsGroup"], pod["volumes"][0]["secret"]["defaultMode"]), (10001, 0o440))

    def test_agent_reaches_only_dns_the_kube_api_and_the_declared_platform_name(self):
        items = {(o["kind"], o["metadata"]["name"]): o for o in json.loads((BUNDLE / "agent.json").read_text())["items"]}
        pod = items[("CronJob", "bravo-findings-agent")]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
        # The T44 target egress Service, named like the platform Service so one server certificate SAN fits both.
        self.assertEqual(env["BRAVO_FINDINGS_URL"], "https://bravo-findings.bravo-security.svc:8443")
        self.assertEqual(env["BRAVO_FINDINGS_CA_FILE"], "/run/bravo/ca/ca.crt")
        self.assertIn({"name": "ca", "configMap": {"name": "bravo-findings-ca"}}, pod["volumes"])
        policy = items[("NetworkPolicy", "bravo-findings-agent")]["spec"]
        self.assertEqual(policy["podSelector"], {"matchLabels": {
            "app.kubernetes.io/name": "bravo-findings-agent", "bravo.dev/findings-client": "agent"}})
        self.assertEqual(policy["ingress"], [])
        # DNS and the API server endpoint after Service DNAT; the tailnet proxy range is the T44 client policy's.
        dns, api = policy["egress"]
        self.assertEqual({p["port"] for p in dns["ports"]}, {53})
        self.assertEqual(api, {"ports": [{"protocol": "TCP", "port": 6443}]})


if __name__ == "__main__":
    unittest.main()
