"""Render the pinned customer chart; bootstrap must not acquire customer workloads."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parent / "argocd"


def render(application=None, *, crds=True, extra=None, skip_schema=False):
    values = {"environment": "target-dev", "tailnet": "example-tail.ts.net", "targetRevision": "a" * 40}
    if application is not None:
        values["application"] = application
    values.update(extra or {})
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "values.json"
        path.write_text(json.dumps(values))
        command = ["helm", "template", "bravo-iac-gitops", str(CHART), "--namespace", "bravo-iac-gitops",
                   "--values", str(path)]
        if crds:
            command += ["--api-versions", "argoproj.io/v1alpha1/Application"]
        if skip_schema:
            command += ["--skip-schema-validation"]
        return subprocess.run(command, check=False, text=True, capture_output=True, timeout=30)


def documents(result):
    if result.returncode:
        raise AssertionError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


class CustomerChart(unittest.TestCase):
    def test_bootstrap_has_no_application_even_when_crds_already_exist(self):
        for crds in (False, True):
            docs = documents(render({"enabled": False}, crds=crds))
            self.assertFalse(any(d["kind"] in {"Application", "AppProject"} for d in docs))
            self.assertTrue(any(d["kind"] == "StatefulSet" for d in docs))
            self.assertFalse(any(d["kind"] in {"Ingress", "HTTPRoute"} for d in docs))

    def test_an_old_upgrade_command_cannot_silently_remove_the_application(self):
        for crds in (False, True):
            for skip_schema in (False, True):
                with self.subTest(crds=crds, skip_schema=skip_schema):
                    result = render(crds=crds, skip_schema=skip_schema)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("enabled", result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_application_can_be_created_paused_without_auto_prune(self):
        docs = documents(render({"enabled": True}))
        app = next(d for d in docs if d["kind"] == "Application")
        self.assertEqual(app["spec"]["syncPolicy"]["automated"], {
            "enabled": False, "prune": False, "selfHeal": True, "allowEmpty": False})
        self.assertEqual(app["spec"]["source"]["targetRevision"], "a" * 40)
        self.assertEqual(app["spec"]["source"]["path"], "infra/iac/charts/target-dev")
        self.assertEqual(app["spec"]["destination"], {"server": "https://kubernetes.default.svc"})

    def test_operator_owned_egress_targets_are_neither_drift_nor_overwritten(self):
        # tailnet-egress Services are created with "placeholder"; the Tailscale operator writes the proxy name.
        app = next(d for d in documents(render({"enabled": True, "autoSync": True})) if d["kind"] == "Application")
        self.assertEqual(app["spec"]["ignoreDifferences"],
                         [{"group": "", "kind": "Service", "jsonPointers": ["/spec/externalName"]}])
        self.assertIn("RespectIgnoreDifferences=true", app["spec"]["syncPolicy"]["syncOptions"])

    def test_activation_and_pruning_are_explicit_independent_choices(self):
        for prune in (False, True):
            docs = documents(render({"enabled": True, "autoSync": True, "prune": prune}))
            app = next(d for d in docs if d["kind"] == "Application")
            self.assertIs(app["spec"]["syncPolicy"]["automated"]["enabled"], True)
            self.assertIs(app["spec"]["syncPolicy"]["automated"]["prune"], prune)

    def test_rejects_inconsistent_or_untyped_modes(self):
        for value in ({"enabled": False, "autoSync": True}, {"enabled": False, "prune": True},
                      {"enabled": True, "autoSync": False, "prune": True}, {"enabled": "true"},
                      {"enabled": True, "unexpected": True}):
            with self.subTest(value=value):
                self.assertNotEqual(render(value).returncode, 0)

    def test_rejects_foreign_or_mutable_sources(self):
        for extra in ({"environment": "platform"}, {"targetRevision": "develop"},
                      {"targetRevision": ""}, {"repoURL": "https://example.invalid/foreign.git"}):
            with self.subTest(extra=extra):
                self.assertNotEqual(render({"enabled": True, "autoSync": True}, extra=extra).returncode, 0)


UI = yaml.safe_load((CHART / "ui-values.yaml").read_text())


class CustomerUi(unittest.TestCase):
    def docs(self, extra=UI):
        return documents(render({"enabled": False}, extra=extra))

    def test_server_is_reachable_only_from_its_tailnet_proxy(self):
        policies = {d["metadata"]["name"]: d["spec"] for d in self.docs() if d["kind"] == "NetworkPolicy"}
        self.assertEqual(set(policies), {f"bravo-iac-gitops-{c}" for c in ("server", "repo-server", "redis", "application-controller")})
        self.assertFalse(any({} in p.get("ingress", []) for p in policies.values()))
        rule, = policies["bravo-iac-gitops-server"]["ingress"]
        peer, = rule["from"]
        self.assertEqual(peer["namespaceSelector"]["matchLabels"], {"kubernetes.io/metadata.name": "tailscale-system"})
        self.assertEqual(peer["podSelector"]["matchLabels"]["tailscale.com/parent-resource"], "bravo-target-argocd")
        self.assertEqual(peer["podSelector"]["matchLabels"]["tailscale.com/parent-resource-ns"], "bravo-iac-gitops")
        self.assertEqual(rule["ports"], [{"port": 8080, "protocol": "TCP"}])

    def test_anonymous_view_is_read_only_without_logs(self):
        docs = self.docs()
        server_roles = [d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"].endswith("-server")]
        self.assertTrue(server_roles)
        for role in server_roles:
            for rule in role["rules"]:
                self.assertLessEqual(set(rule["verbs"]), {"get", "list", "watch"})
        cm = next(d for d in docs if d["metadata"]["name"] == "argocd-cm")["data"]
        self.assertEqual((cm["admin.enabled"], cm["exec.enabled"], cm["users.anonymous.enabled"]), ("false", "false", "true"))
        rbac = next(d for d in docs if d["metadata"]["name"] == "argocd-rbac-cm")["data"]
        self.assertEqual(rbac["policy.default"], "role:bravo-viewer")
        self.assertNotIn("logs", rbac["policy.csv"])
        self.assertNotIn("*/*", rbac["policy.csv"])

    def test_ui_cannot_keep_the_charts_admit_all_server_policy(self):
        extra = json.loads(json.dumps(UI))
        extra["argo-cd"]["global"]["networkPolicy"]["create"] = True
        result = render({"enabled": False}, extra=extra)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("admits every Pod", result.stderr)

    def test_without_ui_the_core_mode_is_unchanged(self):
        docs = documents(render({"enabled": False}))
        self.assertFalse(any(d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-server")
                             and not d["metadata"]["name"].endswith("repo-server") for d in docs))

if __name__ == "__main__":
    unittest.main()
