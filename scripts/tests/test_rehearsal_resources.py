"""T9 rehearsal resources for T15: a separate bucket identity and an OpenBao rehearsal subtree."""
import importlib.util
import json
import re
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
    NS, ACCESSOR = "rehearsal-0123abcd", "auth_kubernetes_0a1b2c3d"

    def test_reader_policy_is_one_templated_policy_scoped_to_the_tokens_own_namespace(self):
        policy = render.rehearsal_reader_policy(self.ACCESSOR)
        self.assertEqual(render.READER_POLICY, "rehearsal-app-reader")
        self.assertIn('path "secret/data/bravo/rehearsal/{{identity.entity.aliases.auth_kubernetes_0a1b2c3d'
                      '.metadata.service_account_namespace}}/*" { capabilities = ["read"] }', policy)
        self.assertNotIn("bravo/rehearsal/*", policy)       # never the whole subtree
        self.assertNotIn("bravo/target", policy)
        self.assertNotIn('"create"', policy)
        self.assertEqual(policy.count('path "secret/'), 1)
        for bad in (None, "", "auth_kubernetes_", "auth_kubernetes_0A1B2C3D", "auth_token_0a1b2c3d",
                    "auth_kubernetes_0a1b2c3d}}", "auth_kubernetes_0a1b2c3d.x", "auth_kubernetes_0a1b2c3d "):
            with self.subTest(accessor=bad), self.assertRaises(ValueError):
                render.rehearsal_reader_policy(bad)
        self.assertFalse(hasattr(render, "rehearsal_policy"))  # the per-run writer-written policy is gone

    def test_auth_role_binds_exact_namespace_and_the_fixed_policy_without_a_selector(self):
        role = render.rehearsal_auth_role(self.NS)
        # Scalars, not lists: the writer's allowed_parameters match strings and exact values only.
        self.assertEqual(role["bound_service_account_names"], "rehearsal-eso-reader")
        self.assertEqual(role["bound_service_account_namespaces"], self.NS)
        self.assertNotIn("bound_service_account_namespace_selector", role)  # review: no namespaces-get RBAC needed
        self.assertEqual(role["token_policies"], "rehearsal-app-reader")
        self.assertEqual((role["token_ttl"], role["token_max_ttl"]), ("300s", "900s"))
        self.assertTrue(role["token_no_default_policy"] and role["audience"] == "openbao")
        self.assertEqual(render.rehearsal_role_name(self.NS), "rehearsal-app-reader-rehearsal-0123abcd")
        self.assertEqual(render.rehearsal_remote_path(self.NS), "bravo/rehearsal/rehearsal-0123abcd/database")
        for bad in ("target-dev", "bravo-rehearsal-control", "rehearsal-control", "rehearsal-control-x", "rehearsal-",
                    "Rehearsal-abc", "rehearsal-a,b", "rehearsal-*"):
            with self.subTest(namespace=bad):
                for function in (render.rehearsal_auth_role, render.rehearsal_role_name, render.rehearsal_remote_path):
                    with self.assertRaises(ValueError):
                        function(bad)

    def test_delivery_targets_the_namespace_path_and_role_under_the_application_secret_name(self):
        doc = render.rehearsal_delivery(self.NS)
        kinds = {i["kind"]: i for i in doc["items"]}
        self.assertEqual(set(kinds), {"ServiceAccount", "SecretStore", "ExternalSecret"})
        self.assertTrue(all(i["metadata"]["namespace"] == self.NS for i in doc["items"]))
        self.assertTrue(all(i["metadata"]["labels"]["app.kubernetes.io/part-of"] == "bravo-rehearsal" for i in doc["items"]))
        secret = kinds["ExternalSecret"]
        self.assertEqual(secret["metadata"]["name"], "target-app-credentials")
        self.assertEqual(secret["spec"]["data"][0]["remoteRef"]["key"], "bravo/rehearsal/rehearsal-0123abcd/database")
        self.assertEqual(secret["spec"]["target"]["deletionPolicy"], "Delete")
        self.assertEqual(kinds["SecretStore"]["spec"]["provider"]["vault"]["auth"]["kubernetes"]["role"],
                         "rehearsal-app-reader-rehearsal-0123abcd")
        for bad in ("target-dev", "rehearsal-", "Rehearsal-abc", "bravo-secrets", "bravo-rehearsal-control", "rehearsal-control"):
            with self.assertRaises(ValueError):
                render.rehearsal_delivery(bad)

    def test_init_script_is_atomic_restartable_and_always_revokes(self):
        script = render.rehearsal_fetch_script(self.NS)
        self.assertIn("auth/kubernetes/login role=rehearsal-app-reader-rehearsal-0123abcd", script)
        self.assertIn("-mount=secret bravo/rehearsal/rehearsal-0123abcd/database", script)
        self.assertIn("umask 077", script)
        self.assertIn('> "$TMP"', script)                       # private temp file, never the target directly
        self.assertIn('mv -f "$TMP" "$OUT"', script)            # atomic replace works over an earlier 0400 file
        self.assertIn("trap cleanup EXIT", script)             # revoke attempted on every exit path
        self.assertIn("token revoke -self", script)
        self.assertIn('[ -s "$TMP" ]', script)                 # empty fetch is a failure, not a stale success
        self.assertNotIn("> /run/bravo/database/credentials.json", script)
        container = render.rehearsal_fetch_init_container(self.NS)
        self.assertEqual(container["command"][2], script)
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertEqual(container["x-bravo-remote-path"], "secret/data/bravo/rehearsal/rehearsal-0123abcd/database")
        self.assertNotIn("password\": \"", json.dumps(container))
        with self.assertRaises(ValueError):
            render.rehearsal_fetch_script("bravo-rehearsal-control")

    def test_init_script_restart_on_same_volume_succeeds(self):
        """Run the generated shell twice against a fake ``bao`` on the same directory: the second
        run must replace the 0400 file from the first, and a failing fetch must leave no file."""
        import os
        import stat
        import subprocess
        import tempfile
        script = render.rehearsal_fetch_script(self.NS)
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
        from contextlib import redirect_stderr, redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            render.main(["rehearsal-delivery", "--namespace", self.NS])
        self.assertEqual(json.loads(out.getvalue())["kind"], "List")
        out = io.StringIO()
        with redirect_stdout(out):
            render.main(["rehearsal-auth-role", "--namespace", self.NS])
        self.assertEqual(json.loads(out.getvalue())["bound_service_account_namespaces"], self.NS)
        out = io.StringIO()
        with redirect_stdout(out):
            render.main(["rehearsal-reader-policy", "--kubernetes-accessor", self.ACCESSOR])
        self.assertEqual(out.getvalue(), render.rehearsal_reader_policy(self.ACCESSOR))
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):  # removed per-run stage is refused
            render.main(["rehearsal-policy", "--run-id", "run-demo-1"])


def _stanzas(policy):
    """Path -> stanza text of the rendered HCL (one top-level ``path`` block per stanza)."""
    blocks, current = {}, None
    for line in policy.splitlines():
        match = re.match(r'^path "([^"]+)"', line)
        if match:
            current = match.group(1)
            blocks[current] = line
        elif current:
            blocks[current] += "\n" + line
    return blocks


def _openbao_glob(item, value):
    """OpenBao 2.7.2 ``GlobbedStringsMatch``: ``*x*`` contains, ``x*`` prefix, ``*x`` suffix."""
    if len(item) < 2:
        return value == item
    if item.startswith("*") and item.endswith("*"):
        return item[1:-1] in value
    if item.endswith("*"):
        return value.startswith(item[:-1])
    if item.startswith("*"):
        return value.endswith(item[1:])
    return value == item


ROLE_PATH = "auth/kubernetes/role/rehearsal-app-reader-*"


class RehearsalWriterTests(unittest.TestCase):
    """The T15 prep Job's OpenBao identity (bravo-rehearsal-control / rehearsal-openbao-writer)."""

    def test_writer_policy_grants_exactly_the_agreed_paths(self):
        policy = render.rehearsal_writer_policy()
        blocks = _stanzas(policy)
        self.assertEqual(set(blocks), {
            "secret/data/bravo/rehearsal/*", "secret/metadata/bravo/rehearsal/*", ROLE_PATH,
            "auth/token/lookup-self", "auth/token/renew-self", "auth/token/revoke-self"})
        crud = 'capabilities = ["create", "read", "update", "delete"]'
        for path in ("secret/data/bravo/rehearsal/*", "secret/metadata/bravo/rehearsal/*"):
            self.assertIn(crud, blocks[path])
        for forbidden in ("sys/polic", "identity/", "bravo/target", "sys/auth", "sys/mounts", "auth/token/create",
                          '"list"', '"sudo"', "+", 'path "*"'):
            self.assertNotIn(forbidden, policy)

    def test_writer_role_path_constrains_every_reader_role_field(self):
        block = _stanzas(render.rehearsal_writer_policy())[ROLE_PATH]
        self.assertIn('capabilities = ["create", "update", "delete"]', block)  # read always fails with required_parameters
        required = json.loads(re.search(r"required_parameters = (\[.*\])", block).group(1))
        self.assertEqual(set(required), set(render.rehearsal_auth_role("rehearsal-abc")))
        for line in ('"bound_service_account_names" = ["rehearsal-eso-reader"]', '"audience" = ["openbao"]',
                     '"bound_service_account_namespaces" = ["rehearsal-*"]', '"token_policies" = ["rehearsal-app-reader"]',
                     '"token_ttl" = ["300s"]', '"token_max_ttl" = ["900s"]', '"token_no_default_policy" = [true]',
                     '"bound_service_account_namespaces" = ["rehearsal-control*", "*,*", "***", "* *"]'):
            self.assertIn(line, block)
        self.assertNotIn("rehearsal-app-reader*", block.split("allowed_parameters")[1])  # policy value is exact, no glob

    @staticmethod
    def _parameters(text):
        return {k: json.loads(v) for k, v in re.findall(r'^    "(\w+)" = (\[.*\])$', text, re.MULTILINE)}

    @staticmethod
    def _in_list(value, choices):
        """OpenBao ``valueInSlice``: two strings glob-match, anything else must be equal."""
        return any(_openbao_glob(c, value) if isinstance(c, str) and isinstance(value, str) else c == value
                   for c in choices)

    def _passes(self, body):
        allowed_part, denied_part = _stanzas(render.rehearsal_writer_policy())[ROLE_PATH].split("denied_parameters")
        allowed, denied = self._parameters(allowed_part), self._parameters(denied_part)
        return all(key in allowed and self._in_list(value, allowed[key])
                   and not self._in_list(value, denied.get(key, [])) for key, value in body.items())

    def test_rendered_reader_role_passes_and_known_bypasses_do_not(self):
        """A model of OpenBao's parameter check; the real server result is in the T9 evidence."""
        role = render.rehearsal_auth_role("rehearsal-0123456789abcdef01234567")
        self.assertTrue(self._passes(role))
        for key, value in [("bound_service_account_namespaces", "bravo-rehearsal-control"),
                           ("bound_service_account_namespaces", "rehearsal-control"),
                           ("bound_service_account_namespaces", "rehearsal-control "),
                           ("bound_service_account_namespaces", "rehearsal-a,target-dev"),
                           ("bound_service_account_namespaces", "rehearsal-*"),
                           ("bound_service_account_namespaces", ["rehearsal-a"]),
                           ("bound_service_account_names", "rehearsal-openbao-writer"),
                           ("token_policies", "rehearsal-app-reader,default"), ("token_policies", "default"),
                           ("token_policies", "rehearsal-app-reader-evil"), ("token_policies", "rehearsal-app-reader "),
                           ("token_policies", ["rehearsal-app-reader"]), ("token_policies", "rehearsal-openbao-writer"),
                           ("token_ttl", "768h"), ("token_no_default_policy", False), ("token_period", "24h")]:
            with self.subTest(key=key, value=value):
                self.assertFalse(self._passes(dict(role, **{key: value})))

    def test_writer_auth_role_binds_exactly_the_writer_sa(self):
        role = render.rehearsal_writer_auth_role()
        self.assertEqual(role, {"bound_service_account_names": "rehearsal-openbao-writer",
                                "bound_service_account_namespaces": "bravo-rehearsal-control", "audience": "openbao",
                                "token_policies": "rehearsal-openbao-writer", "token_ttl": "300s",
                                "token_max_ttl": "900s", "token_no_default_policy": True})
        self.assertFalse(role["token_policies"].startswith(render.READER_POLICY))  # not a reader policy name

    def test_optional_identity_is_not_a_rehearsal_attempt_namespace(self):
        namespace, account = render.rehearsal_writer_identity()["items"]
        self.assertEqual((namespace["kind"], namespace["metadata"]["name"]), ("Namespace", "bravo-rehearsal-control"))
        self.assertNotEqual(namespace["metadata"]["labels"].get("app.kubernetes.io/part-of"), "bravo-rehearsal")
        self.assertEqual(namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "restricted")
        self.assertEqual((account["kind"], account["metadata"]["name"], account["metadata"]["namespace"]),
                         ("ServiceAccount", "rehearsal-openbao-writer", "bravo-rehearsal-control"))
        self.assertFalse(account["automountServiceAccountToken"])

    def test_writer_cli_stages(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            render.main(["rehearsal-writer-policy"])
        self.assertEqual(out.getvalue(), render.rehearsal_writer_policy())
        for stage, kind in (("rehearsal-writer-auth-role", None), ("rehearsal-writer-identity", "List")):
            out = io.StringIO()
            with redirect_stdout(out):
                render.main([stage])
            self.assertEqual(json.loads(out.getvalue()).get("kind"), kind)


if __name__ == "__main__":
    unittest.main()
