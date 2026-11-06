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

    def test_t31_apply_mounts_are_off_by_default_and_added_only_together(self):
        base = render(VALUES)[1]
        explicit = render(VALUES | {"application": {"targetKubeconfigSecret": "", "receiptsClaim": ""}})[1]
        self.assertEqual(base, explicit)
        pod = next(d for d in base if d["kind"] == "Deployment")["spec"]["template"]
        self.assertNotIn("bravo.dev/target-api-client", pod["metadata"]["labels"])
        self.assertFalse({"target-kubeconfig", "t31-receipts"} & {v["name"] for v in pod["spec"]["volumes"]})
        on = VALUES | {"application": {"targetKubeconfigSecret": "t31-target", "receiptsClaim": "t31-records"}}
        result, docs = render(on)
        self.assertEqual(result.returncode, 0, result.stderr)
        template = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]
        pod = template["spec"]
        self.assertEqual(template["metadata"]["labels"]["bravo.dev/target-api-client"], "apply")
        self.assertIs(pod["automountServiceAccountToken"], False)  # never the host's own API identity
        volumes = {v["name"]: v for v in pod["volumes"]}
        secret = volumes["target-kubeconfig"]["secret"]
        self.assertEqual((secret["secretName"], secret["optional"], secret["items"]),
                         ("t31-target", True, [{"key": "kubeconfig", "path": "kubeconfig"}]))
        self.assertEqual(volumes["t31-receipts"]["persistentVolumeClaim"]["claimName"], "t31-records")
        mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
        self.assertEqual(mounts["target-kubeconfig"]["mountPath"], "/var/run/secrets/bravo-t31-target")
        self.assertTrue(mounts["target-kubeconfig"]["readOnly"])
        self.assertEqual(mounts["t31-receipts"]["mountPath"], "/var/lib/bravo/t31")
        for half in ({"targetKubeconfigSecret": "t31-target"}, {"receiptsClaim": "t31-records"}):
            self.assertNotEqual(render(VALUES | {"application": half})[0].returncode, 0, half)

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

        relay = json.dumps({"job_principal": "b" * 64, "fence_principals": ["d" * 64]})
        data = {"configmap": {"workflow-config": {"ports.json": "{}", "relay.json": relay},
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
        for bad in ({}, {"job_principal": "b" * 64, "fence_principals": []},
                    {"job_principal": "short", "fence_principals": ["d" * 64]},
                    {"job_principal": "b" * 64, "fence_principals": ["b" * 64]}):
            data["configmap"]["workflow-config"] = {"ports.json": "{}", "relay.json": json.dumps(bad)}
            with self.assertRaisesRegex(ValueError, "relay_principals_invalid"):
                bootstrap.verify_workflow_host(get, host)
        data["configmap"]["workflow-config"] = {"ports.json": "{}"}
        with self.assertRaisesRegex(ValueError, "relay"):
            bootstrap.verify_workflow_host(get, host)
        with self.assertRaisesRegex(ValueError, "fields"):
            bootstrap.verify_workflow_host(get, {"enabled": True, "runtime": {"configuration": {}}})

    def test_bootstrap_t31_application_needs_apply_config_and_receipt_claim(self):
        import bootstrap

        relay = json.dumps({"job_principal": "b" * 64, "fence_principals": ["d" * 64]})
        config = {"ports.json": "{}", "relay.json": relay, "apply.json": "{}"}
        seen = []

        def get(kind, name, namespace):
            seen.append((kind, name))
            data = {"workflow-config": config, "reader-config-v2": {"reader.json": json.dumps({"version": 2})}}
            return {"metadata": {"name": name}, "data": data.get(name, {})}

        runtime = {k: VALUES[k] for k in ("configuration", "reader", "stateClaim", "secrets")}
        application = {"targetKubeconfigSecret": "t31-target", "receiptsClaim": "t31-records"}
        bootstrap.verify_workflow_host(get, {"enabled": True, "runtime": runtime | {"application": application}})
        self.assertIn(("pvc", "t31-records"), seen)
        self.assertNotIn(("secret", "t31-target"), seen)  # optional mount: a missing Secret is a named hold
        for bad in ({"targetKubeconfigSecret": "t31-target"}, {"targetKubeconfigSecret": "", "receiptsClaim": "r"},
                    {"targetKubeconfigSecret": "t", "receiptsClaim": "r", "kubeconfig": "inline"}):
            with self.assertRaisesRegex(ValueError, "application_fields"):
                bootstrap.verify_workflow_host(get, {"enabled": True, "runtime": runtime | {"application": bad}})
        del config["apply.json"]
        with self.assertRaisesRegex(ValueError, "apply_config"):
            bootstrap.verify_workflow_host(get, {"enabled": True, "runtime": runtime | {"application": application}})

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
