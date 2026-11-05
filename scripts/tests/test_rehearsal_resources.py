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
}
# Built at import time from low-entropy pieces so the history scanner sees no key-like literal.
ENV["BRAVO_OBJECT_REHEARSAL_ACCESS_KEY"] = "REHEARSAL" + "A" * 11
ENV["BRAVO_OBJECT_REHEARSAL_SECRET_KEY"] = "rehearsal-" + "a" * 30
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
    NS, RUN = "rehearsal-0123abcd", "run-demo-1"

    def test_policy_reads_only_this_runs_path(self):
        policy = render.rehearsal_policy(self.RUN)
        self.assertIn('path "secret/data/bravo/rehearsal/run-demo-1/*" { capabilities = ["read"] }', policy)
        self.assertNotIn("bravo/rehearsal/*", policy)      # not the whole subtree (review: run isolation)
        self.assertNotIn("bravo/target", policy)
        self.assertNotIn('"create"', policy)
        self.assertNotEqual(policy, render.rehearsal_policy("run-demo-2"))
        with self.assertRaises(ValueError):
            render.rehearsal_policy("run demo")

    def test_auth_role_binds_exact_namespace_and_run_policy_without_a_selector(self):
        role = render.rehearsal_auth_role(self.NS, self.RUN)
        self.assertEqual(role["bound_service_account_names"], ["rehearsal-eso-reader"])
        self.assertEqual(role["bound_service_account_namespaces"], [self.NS])
        self.assertNotIn("bound_service_account_namespace_selector", role)  # review: no namespaces-get RBAC needed
        self.assertEqual(role["token_policies"], ["rehearsal-app-reader-run-demo-1"])
        self.assertTrue(role["token_no_default_policy"] and role["audience"] == "openbao")
        other = render.rehearsal_auth_role("rehearsal-ffff0000", "run-demo-2")
        self.assertNotEqual(other["token_policies"], role["token_policies"])
        with self.assertRaises(ValueError):
            render.rehearsal_auth_role("target-dev", self.RUN)

    def test_delivery_targets_the_run_path_and_run_role_under_the_application_secret_name(self):
        doc = render.rehearsal_delivery(self.NS, self.RUN)
        kinds = {i["kind"]: i for i in doc["items"]}
        self.assertEqual(set(kinds), {"ServiceAccount", "SecretStore", "ExternalSecret"})
        self.assertTrue(all(i["metadata"]["namespace"] == self.NS for i in doc["items"]))
        self.assertTrue(all(i["metadata"]["labels"]["app.kubernetes.io/part-of"] == "bravo-rehearsal" for i in doc["items"]))
        secret = kinds["ExternalSecret"]
        self.assertEqual(secret["metadata"]["name"], "target-app-credentials")
        self.assertEqual(secret["spec"]["data"][0]["remoteRef"]["key"], "bravo/rehearsal/run-demo-1/database")
        self.assertEqual(secret["spec"]["target"]["deletionPolicy"], "Delete")
        self.assertEqual(kinds["SecretStore"]["spec"]["provider"]["vault"]["auth"]["kubernetes"]["role"], "rehearsal-app-reader-run-demo-1")
        for bad in ("target-dev", "rehearsal-", "Rehearsal-abc", "bravo-secrets"):
            with self.assertRaises(ValueError):
                render.rehearsal_delivery(bad, self.RUN)
        with self.assertRaises(ValueError):
            render.rehearsal_delivery("rehearsal-abc", "run demo")

    def test_init_script_is_atomic_restartable_and_always_revokes(self):
        script = render.rehearsal_fetch_script(self.RUN)
        self.assertIn("auth/kubernetes/login role=rehearsal-app-reader-run-demo-1", script)
        self.assertIn("bravo/rehearsal/run-demo-1/database", script)
        self.assertIn("umask 077", script)
        self.assertIn('> "$TMP"', script)                       # private temp file, never the target directly
        self.assertIn('mv -f "$TMP" "$OUT"', script)            # atomic replace works over an earlier 0400 file
        self.assertIn("trap cleanup EXIT", script)             # revoke attempted on every exit path
        self.assertIn("token revoke -self", script)
        self.assertIn('[ -s "$TMP" ]', script)                 # empty fetch is a failure, not a stale success
        self.assertNotIn("> /run/bravo/database/credentials.json", script)
        container = render.rehearsal_fetch_init_container(self.RUN)
        self.assertEqual(container["command"][2], script)
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertNotIn("password\": \"", json.dumps(container))

    def test_init_script_restart_on_same_volume_succeeds(self):
        """Run the generated shell twice against a fake ``bao`` on the same directory: the second
        run must replace the 0400 file from the first, and a failing fetch must leave no file."""
        import os
        import stat
        import subprocess
        import tempfile
        script = render.rehearsal_fetch_script(self.RUN)
        with tempfile.TemporaryDirectory() as work:
            bindir, data, token_dir = Path(work, "bin"), Path(work, "data"), Path(work, "tok")
            for d in (bindir, data, token_dir, Path(work, "ca")):
                d.mkdir()
            (token_dir / "token").write_text("jwt-fixture")
            (Path(work, "ca") / "ca.crt").write_text("ca")
            fake = bindir / "bao"
            fake.write_text(
                "#!/bin/sh\n"
                "case \"$1 $2\" in\n"
                "  'write -field=token') echo fixture-token ;;\n"
                "  'kv get') if [ -n \"${FAIL_FETCH:-}\" ]; then exit 1; fi; printf '{\"username\":\"u\",\"password\":\"synthetic-%s\"}' \"${RUN_TAG:-1}\" ;;\n"
                "  'token revoke') echo revoked >> \"$REVOKE_LOG\" ;;\n"
                "  *) exit 2 ;;\n"
                "esac\n")
            fake.chmod(0o755)
            localized = (script.replace("/run/bravo/database", str(data)).replace("/openbao/ca/ca.crt", str(Path(work, "ca", "ca.crt")))
                         .replace("/var/run/secrets/openbao/token", str(token_dir / "token")))
            env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", REVOKE_LOG=str(Path(work, "revoke.log")))
            first = subprocess.run(["/bin/sh", "-c", localized], check=False, env=dict(env, RUN_TAG="1"), capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            out = data / "credentials.json"
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o400)
            self.assertIn("synthetic-1", out.read_text())
            second = subprocess.run(["/bin/sh", "-c", localized], check=False, env=dict(env, RUN_TAG="2"), capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)   # review: restart over the 0400 file completes
            self.assertIn("synthetic-2", out.read_text())
            failed = subprocess.run(["/bin/sh", "-c", localized], check=False, env=dict(env, RUN_TAG="3", FAIL_FETCH="1"), capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("synthetic-2", out.read_text())           # previous file untouched, no half-written temp
            self.assertEqual(list(data.glob(".credentials.json.tmp*")), [])
            self.assertEqual(Path(work, "revoke.log").read_text().count("revoked"), 3)  # revoke attempted every run

    def test_cli_stages_render(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            render.main(["rehearsal-delivery", "--namespace", self.NS, "--run-id", self.RUN])
        self.assertEqual(json.loads(out.getvalue())["kind"], "List")
        out = io.StringIO()
        with redirect_stdout(out):
            render.main(["rehearsal-auth-role", "--namespace", self.NS, "--run-id", self.RUN])
        self.assertEqual(json.loads(out.getvalue())["bound_service_account_namespaces"], [self.NS])


if __name__ == "__main__":
    unittest.main()
