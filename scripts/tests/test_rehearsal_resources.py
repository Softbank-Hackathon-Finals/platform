"""T9 rehearsal resources for T15: a separate bucket identity and an OpenBao rehearsal subtree."""
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exposure = _module("object_exposure", "infra/envs/target-workloads/object_exposure.py")
render = _module("target_secrets_render", "infra/envs/target-secrets/render.py")

ENV = {
    "BRAVO_OBJECT_BOOTSTRAP_ACCESS_KEY": "BOOTSTRAPFIXTURE0001", "BRAVO_OBJECT_BOOTSTRAP_SECRET_KEY": "bootstrap-fixture-secret-value-not-real-0001",
    "BRAVO_OBJECT_CLIENT_ACCESS_KEY": "CLIENTFIXTURE0000001", "BRAVO_OBJECT_CLIENT_SECRET_KEY": "client-fixture-secret-value-not-real-000001",
    "BRAVO_OBJECT_REHEARSAL_ACCESS_KEY": "REHEARSALFIXTURE0001", "BRAVO_OBJECT_REHEARSAL_SECRET_KEY": "rehearsal-fixture-secret-value-not-real-01",
}
APP, REH = "demo-chat-attachments", "rehearsal-attachments"


class RehearsalBucketIdentityTests(unittest.TestCase):
    def test_rehearsal_identity_administers_only_its_own_bucket(self):
        doc = exposure.render("closed", APP, ENV, rehearsal_bucket=REH)
        names = [i["name"] for i in doc["identities"]]
        self.assertEqual(names, ["bootstrap", "client", "rehearsal-object-admin"])
        rehearsal = doc["identities"][2]
        self.assertEqual(set(rehearsal["actions"]), {f"{a}:{REH}" for a in ("Admin", "Read", "Write", "List", "Tagging")})
        self.assertFalse(any(APP in action for action in rehearsal["actions"]))
        for identity in doc["identities"][:2]:
            self.assertFalse(any(REH in action for action in identity["actions"]))

    def test_rehearsal_bucket_must_differ_and_needs_its_own_credentials(self):
        with self.assertRaises(ValueError):
            exposure.render("closed", APP, ENV, rehearsal_bucket=APP)
        missing = {k: v for k, v in ENV.items() if "REHEARSAL" not in k}
        with self.assertRaises(ValueError):
            exposure.render("closed", APP, missing, rehearsal_bucket=REH)
        same = dict(ENV, BRAVO_OBJECT_REHEARSAL_ACCESS_KEY=ENV["BRAVO_OBJECT_CLIENT_ACCESS_KEY"])
        with self.assertRaises(ValueError):
            exposure.render("closed", APP, same, rehearsal_bucket=REH)
        self.assertEqual(len(exposure.render("closed", APP, ENV)["identities"]), 2)  # default unchanged


class RehearsalSecretsTests(unittest.TestCase):
    def test_policy_reads_only_the_rehearsal_subtree(self):
        policy = render.rehearsal_policy()
        self.assertIn('path "secret/data/bravo/rehearsal/*" { capabilities = ["read"] }', policy)
        self.assertNotIn("bravo/target", policy)
        self.assertNotIn('"create"', policy)
        self.assertNotIn('"update" }', policy.split("renew-self")[0])  # no KV writes

    def test_auth_role_binds_reader_account_in_labelled_rehearsal_namespaces(self):
        role = render.rehearsal_auth_role()
        self.assertEqual(role["bound_service_account_names"], ["rehearsal-eso-reader"])
        self.assertEqual(json.loads(role["bound_service_account_namespace_selector"]), {"matchLabels": {"app.kubernetes.io/part-of": "bravo-rehearsal"}})
        self.assertEqual(role["token_policies"], ["rehearsal-app-reader"])
        self.assertTrue(role["token_no_default_policy"] and role["audience"] == "openbao")

    def test_delivery_targets_the_run_path_under_the_application_secret_name(self):
        doc = render.rehearsal_delivery("rehearsal-0123abcd", "run-demo-1")
        kinds = {i["kind"]: i for i in doc["items"]}
        self.assertEqual(set(kinds), {"ServiceAccount", "SecretStore", "ExternalSecret"})
        self.assertTrue(all(i["metadata"]["namespace"] == "rehearsal-0123abcd" for i in doc["items"]))
        self.assertTrue(all(i["metadata"]["labels"]["app.kubernetes.io/part-of"] == "bravo-rehearsal" for i in doc["items"]))
        secret = kinds["ExternalSecret"]
        self.assertEqual(secret["metadata"]["name"], "target-app-credentials")
        self.assertEqual(secret["spec"]["data"][0]["remoteRef"]["key"], "bravo/rehearsal/run-demo-1/database")
        self.assertEqual(secret["spec"]["target"]["deletionPolicy"], "Delete")
        self.assertEqual(kinds["SecretStore"]["spec"]["provider"]["vault"]["auth"]["kubernetes"]["role"], "rehearsal-app-reader")
        for bad in ("target-dev", "rehearsal-", "Rehearsal-abc", "bravo-secrets"):
            with self.assertRaises(ValueError):
                render.rehearsal_delivery(bad, "run-demo-1")
        with self.assertRaises(ValueError):
            render.rehearsal_delivery("rehearsal-abc", "run demo")

    def test_init_container_fetches_the_same_path_without_embedding_values(self):
        container = render.rehearsal_fetch_init_container("run-demo-1")
        script = container["command"][2]
        self.assertIn("auth/kubernetes/login role=rehearsal-app-reader", script)
        self.assertIn("bravo/rehearsal/run-demo-1/database", script)
        self.assertIn("/run/bravo/database/credentials.json", script)
        self.assertIn("token revoke -self", script)
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertNotIn("password", json.dumps(container).lower())

    def test_cli_stages_render(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            render.main(["rehearsal-delivery", "--namespace", "rehearsal-0123abcd", "--run-id", "run-demo-1"])
        self.assertEqual(json.loads(out.getvalue())["kind"], "List")


if __name__ == "__main__":
    unittest.main()
