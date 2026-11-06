# /// script
# dependencies = ["pyyaml==6.0.3"]
# ///
"""T10 workflow host: one driver, read-only inputs, no API token, DB TLS, disabled by default in Argo."""
import json
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "infra/charts/workflow-host"
VALUES = {"image": {"digest": "sha256:" + "a" * 64}, "sourceRevision": "b" * 40,
          "configuration": {"existingConfigMap": "workflow-config", "sha256": "c" * 64},
          "reader": {"existingConfigMap": "reader-config-v2", "catalogClaim": "catalog"},
          "stateClaim": "workflow-state",
          "secrets": {"catalogKey": "catalog-key", "executor": "executor-read", "githubApp": "github-app"}}


def render(values, chart=CHART):
    result = subprocess.run(["helm", "template", "wh", str(chart), "-f", "-"], input=json.dumps(values),
                            capture_output=True, text=True, check=False)
    return result, [d for d in yaml.safe_load_all(result.stdout) if d] if result.returncode == 0 else []


class WorkflowHostChart(unittest.TestCase):
    def test_single_driver_with_read_only_inputs_and_no_token(self):
        result, docs = render(VALUES)
        self.assertEqual(result.returncode, 0, result.stderr)
        deployment = next(d for d in docs if d["kind"] == "Deployment")
        spec = deployment["spec"]
        self.assertEqual((spec["replicas"], spec["strategy"]["type"]), (1, "Recreate"))
        pod = spec["template"]["spec"]
        self.assertIs(pod["automountServiceAccountToken"], False)
        container = pod["containers"][0]
        self.assertEqual(container["command"][-1], "bravo_platform_events.workflow_host_server")
        self.assertIn("bravo_platform_events.workflow_ports:create_ports", container["args"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        writable = {m["name"] for m in container["volumeMounts"] if not m.get("readOnly")}
        self.assertEqual(writable, {"state", "tmp"})
        executor = next(v for v in pod["volumes"] if v["name"] == "executor")["secret"]["items"]
        self.assertEqual({i["key"] for i in executor}, {"ca.crt", "read-token"})  # never a command token
        self.assertEqual(deployment["spec"]["template"]["metadata"]["labels"]["bravo.dev/db-client"], "runtime")

    def test_missing_required_inputs_are_refused(self):
        for key in ("image", "configuration", "reader", "stateClaim"):
            values = {k: v for k, v in VALUES.items() if k != key}
            self.assertNotEqual(render(values)[0].returncode, 0, key)
        values = json.loads(json.dumps(VALUES))
        values["secrets"]["executor"] = ""
        self.assertNotEqual(render(values)[0].returncode, 0)

    def test_github_app_is_optional(self):
        values = json.loads(json.dumps(VALUES))
        values["secrets"]["githubApp"] = ""
        result, docs = render(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        self.assertNotIn("github-app", {v["name"] for v in pod["volumes"]})

    def test_argo_application_is_disabled_by_default(self):
        result, docs = render({}, ROOT / "infra/gitops/argocd")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = {d["metadata"]["name"] for d in docs if d.get("kind") == "Application"}
        self.assertNotIn("platform-workflow-host", names)


    def test_enabled_argo_app_owns_only_workflow_host_names(self):
        values = {"workflowHost": {"enabled": True, "runtime": {"configuration": {"existingConfigMap": "c", "sha256": "s"}}}}
        result, docs = render(values, ROOT / "infra/gitops/argocd")
        self.assertEqual(result.returncode, 0, result.stderr)
        roles = [d for d in docs if d.get("kind") in ("Role", "RoleBinding")
                 and "workflow" in d["metadata"]["name"]]
        self.assertEqual({d["metadata"]["name"] for d in roles}, {"platform-gitops-workflow-host"})
        names = {n for d in roles if d["kind"] == "Role" for r in d["rules"] for n in r["resourceNames"]}
        self.assertEqual(names, {"t10-workflow-host"})
        policy = next(d for d in docs if d.get("kind") == "ValidatingAdmissionPolicy"
                      and d["metadata"]["name"] == "platform-gitops-registration-names")
        text = json.dumps(policy)
        self.assertIn("t10-workflow-host", text)
        self.assertIn("serviceAccountName == 't10-workflow-host'", text)


    def test_both_hosts_enabled_render_distinct_rbac(self):
        # sangmu1126 15:02Z (4): the rehearsal and workflow host apps must not render the same RBAC.
        import test_rehearsal_host_chart as rehearsal

        values = {"workflowHost": {"enabled": True, "runtime": {"configuration": {"existingConfigMap": "c", "sha256": "s"}}},
                  "rehearsalHost": {"enabled": True, "runtime": rehearsal.RUNTIME}}
        result, docs = render(values, ROOT / "infra/gitops/argocd")
        self.assertEqual(result.returncode, 0, result.stderr)
        rbac = [(d["kind"], d["metadata"]["name"]) for d in docs if d.get("kind") in ("Role", "RoleBinding")]
        self.assertEqual(len(rbac), len(set(rbac)))
        self.assertIn(("Role", "platform-gitops-workflow-host"), rbac)
        self.assertIn(("Role", "platform-gitops-rehearsal-host"), rbac)


    def test_bootstrap_requires_existing_inputs_v2_reader_and_relay(self):
        import bootstrap

        data = {"configmap": {"workflow-config": {"ports.json": "{}", "relay.json": "{}"},
                              "reader-config-v2": {"reader.json": json.dumps({"version": 2})}}}

        def get(kind, name, namespace):
            self.assertEqual(namespace, "platform-state")
            return {"metadata": {"name": name}, "data": data.get(kind, {}).get(name, {})}

        host = {"enabled": True, "runtime": {k: VALUES[k] for k in ("configuration", "reader", "stateClaim", "secrets")}}
        bootstrap.verify_workflow_host(get, host)
        data["configmap"]["reader-config-v2"] = {"reader.json": json.dumps({"version": 1})}
        with self.assertRaisesRegex(ValueError, "reader_v2"):
            bootstrap.verify_workflow_host(get, host)
        data["configmap"]["reader-config-v2"] = {"reader.json": json.dumps({"version": 2})}
        data["configmap"]["workflow-config"] = {"ports.json": "{}"}
        with self.assertRaisesRegex(ValueError, "relay"):
            bootstrap.verify_workflow_host(get, host)
        with self.assertRaisesRegex(ValueError, "fields"):
            bootstrap.verify_workflow_host(get, {"enabled": True, "runtime": {"configuration": {}}})

    def test_bootstrap_initial_proposal_takes_only_a_public_ca(self):
        import bootstrap

        bootstrap.verify_initial_proposal({"enabled": True, "brokerCaPem": "-----BEGIN CERTIFICATE-----\nx"})
        for bad in ({"enabled": True, "brokerCaPem": "-----BEGIN PRIVATE KEY-----"},
                    {"enabled": True, "brokerCaPem": "-----BEGIN CERTIFICATE-----", "extra": 1},
                    {"enabled": False, "brokerCaPem": "-----BEGIN CERTIFICATE-----"}):
            with self.assertRaises(ValueError):
                bootstrap.verify_initial_proposal(bad)


if __name__ == "__main__":
    unittest.main()
