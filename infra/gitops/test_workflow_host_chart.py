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
        self.assertEqual(deployment["spec"]["template"]["metadata"]["labels"]["bravo.dev/target-api-client"], "apply")

    def test_ai_job_automation_adds_only_a_projected_api_token(self):
        result, docs = render(VALUES | {"initialProposalAuto": {"enabled": True}})
        self.assertEqual(result.returncode, 0, result.stderr)
        pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        self.assertIs(pod["automountServiceAccountToken"], False)  # no default token: only this projection
        token = next(v for v in pod["volumes"] if v["name"] == "kube-api-token")["projected"]["sources"]
        self.assertEqual(token[0]["serviceAccountToken"]["expirationSeconds"], 3600)
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "kube-api-token")
        self.assertTrue(mount["readOnly"])
        self.assertFalse(any(d["kind"] in ("Role", "ClusterRole", "RoleBinding") for d in docs))  # RBAC lives with bravo-initial
        _, off = render(VALUES)
        off_pod = next(d for d in off if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        self.assertNotIn("kube-api-token", {v["name"] for v in off_pod["volumes"]})

    def test_candidate_pull_mounts_only_the_registry_credential_read_only(self):
        result, docs = render(VALUES | {"candidatePull": {"enabled": True}})
        self.assertEqual(result.returncode, 0, result.stderr)
        pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        volume = next(v for v in pod["volumes"] if v["name"] == "candidate-pull")["secret"]
        self.assertEqual((volume["secretName"], volume["items"]), ("platform-ghcr-reader",
                         [{"key": ".dockerconfigjson", "path": ".dockerconfigjson"}]))
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "candidate-pull")
        self.assertEqual((mount["mountPath"], mount["readOnly"]), ("/ghcr", True))
        _, off = render(VALUES)
        off_pod = next(d for d in off if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        self.assertNotIn("candidate-pull", {v["name"] for v in off_pod["volumes"]})

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

    def test_apply_credential_mounts_exec_config_and_excludes_the_token_secret(self):
        values = json.loads(json.dumps(VALUES)) | {"applyCredential": {"existingConfigMap": "t31-apply-credential"}}
        result, docs = render(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        pod = next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]
        volume = next(v for v in pod["volumes"] if v["name"] == "apply-kubeconfig")
        self.assertEqual(volume["configMap"]["name"], "t31-apply-credential")
        self.assertEqual({i["path"] for i in volume["configMap"]["items"]}, {"kubeconfig", "registration.json"})
        self.assertNotIn("secret", volume)
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "apply-kubeconfig")
        self.assertEqual((mount["mountPath"], mount["readOnly"]), ("/t31", True))
        values["secrets"]["applyKubeconfig"] = "t31-token"
        self.assertNotEqual(render(values)[0].returncode, 0)

    def test_bootstrap_apply_credential_is_the_exec_kubeconfig_only(self):
        import sys
        sys.path.insert(0, str(ROOT / "infra/envs/rehearsal-access"))
        import bootstrap
        import credentials

        uid = "00000000-0000-4000-8000-00000000000"
        registration = {"cluster_uid": uid + "1", "namespace": "bravo-apply-access", "service_account": "apply-deployer",
                        "service_account_uid": uid + "2", "issuer_group": "tag:bravo-apply",
                        "audience": "https://kubernetes.default.svc.cluster.local",
                        "issuer": {"server": "https://target-credentials.bravo-apply-control.svc.cluster.local",
                                   "tls_server_name": "bravo-target-credentials.example.ts.net"},
                        "target": {"server": "https://target-api.bravo-apply-control.svc.cluster.local",
                                   "tls_server_name": "bravo-target-api.example.ts.net"}}
        kubeconfig = credentials.kubeconfig(credentials.Registration.parse(registration), bootstrap.APPLY_CONFIG,
                                            bootstrap.APPLY_PLUGIN)
        data = {"kubeconfig": json.dumps(kubeconfig),
                "registration.json": json.dumps({"registration": registration, "anchor_uid": uid + "3"})}

        def get(kind, name, namespace):
            return {"data": data}

        bootstrap.verify_apply_credential(get, "t31-apply-credential", {"executor": "x"})
        with self.assertRaisesRegex(ValueError, "exclusive"):
            bootstrap.verify_apply_credential(get, "t31-apply-credential", {"applyKubeconfig": "t31-token"})
        token = json.loads(json.dumps(kubeconfig))
        token["users"][0]["user"] = {"token": "x"}
        data["kubeconfig"] = json.dumps(token)
        with self.assertRaisesRegex(ValueError, "exec_only"):
            bootstrap.verify_apply_credential(get, "t31-apply-credential", {"executor": "x"})
        data["kubeconfig"], data["extra"] = json.dumps(kubeconfig), "x"
        with self.assertRaisesRegex(ValueError, "fields"):
            bootstrap.verify_apply_credential(get, "t31-apply-credential", {"executor": "x"})

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


    def test_the_host_may_run_the_ai_job_and_refresh_the_pull_secret_but_nothing_else(self):
        runtime = {"configuration": {"existingConfigMap": "c", "sha256": "s"},
                   "initialProposalAuto": {"enabled": True}, "candidatePull": {"enabled": True}}
        result, docs = render({"workflowHost": {"enabled": True, "runtime": runtime}}, ROOT / "infra/gitops/argocd")
        self.assertEqual(result.returncode, 0, result.stderr)
        app = next(d for d in docs if d.get("kind") == "Application" and d["metadata"]["name"] == "platform-workflow-host")
        values = app["spec"]["source"]["helm"]["valuesObject"]
        self.assertEqual((values["initialProposalAuto"], values["candidatePull"]), ({"enabled": True}, {"enabled": True}))
        refused, _ = render({"workflowHost": {"enabled": True, "runtime": runtime | {"image": {"digest": "x"}}}},
                            ROOT / "infra/gitops/argocd")
        self.assertNotEqual(refused.returncode, 0)  # the image stays the reviewed pin, never a runtime value

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

    def test_bootstrap_initial_proposal_takes_only_a_public_ca(self):
        import bootstrap

        seen = []

        def get(kind, name, namespace=None):
            seen.append((kind, name, namespace))
            return {}

        bootstrap.verify_initial_proposal(get, {"enabled": True, "brokerCaPem": "-----BEGIN CERTIFICATE-----\nx"})
        self.assertEqual(seen, [("secret", "platform-ghcr-reader", "bravo-initial")])

        def missing(kind, name, namespace=None):
            raise subprocess.CalledProcessError(1, ["kubectl", "get", kind, name])

        with self.assertRaises(subprocess.CalledProcessError):
            bootstrap.verify_initial_proposal(missing, {"enabled": True, "brokerCaPem": "-----BEGIN CERTIFICATE-----\nx"})
        for bad in ({"enabled": True, "brokerCaPem": "-----BEGIN PRIVATE KEY-----"},
                    {"enabled": True, "brokerCaPem": "-----BEGIN CERTIFICATE-----", "extra": 1},
                    {"enabled": False, "brokerCaPem": "-----BEGIN CERTIFICATE-----"}):
            with self.assertRaises(ValueError):
                bootstrap.verify_initial_proposal(get, bad)


if __name__ == "__main__":
    unittest.main()
