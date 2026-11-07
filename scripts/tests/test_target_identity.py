"""T30 target sign-in server: Keycloak replaces the source Cognito user pool by a settings change.

The render tests pin what the browser and demo-chat rely on. The container test (BRAVO_TEST_KEYCLOAK=1)
starts the rendered image with the rendered settings and realm, and asks it for the issuer demo-chat expects.
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

WORKLOADS = Path(__file__).resolve().parents[2] / "infra/envs/target-workloads"
with patch.object(sys, "path", [str(WORKLOADS), *sys.path]), patch.dict(sys.modules):
    for shadowed in ("render", "identity", "identity_users"):
        sys.modules.pop(shadowed, None)
    render = importlib.import_module("render")
    identity = importlib.import_module("identity")
    identity_users = importlib.import_module("identity_users")

PUBLIC = "https://login.example.com"
APP = "https://chat.example.com"


def rendered():
    return {item["kind"]: item for item in identity.identity("node-a", PUBLIC, APP)}


def container(items):
    return items["Deployment"]["spec"]["template"]["spec"]["containers"][0]


class RealmTests(unittest.TestCase):
    def realm(self):
        return json.loads(rendered()["ConfigMap"]["data"]["bravo-realm.json"])

    def test_issuer_is_what_demo_chat_gets_as_auth_issuer(self):
        self.assertEqual(identity.issuer(PUBLIC), "https://login.example.com/realms/bravo")

    def test_one_public_pkce_client_with_exact_callback(self):
        (client,) = self.realm()["clients"]
        self.assertEqual(client["clientId"], "demo-chat")
        self.assertIs(client["publicClient"], True)
        self.assertIs(client["standardFlowEnabled"], True)
        # The app never sees a password and never gets a token without a browser sign-in.
        self.assertIs(client["directAccessGrantsEnabled"], False)
        self.assertIs(client["implicitFlowEnabled"], False)
        self.assertIs(client["serviceAccountsEnabled"], False)
        self.assertEqual(client["redirectUris"], [f"{APP}/"])
        self.assertEqual(client["webOrigins"], [APP])
        self.assertEqual(client["attributes"]["pkce.code.challenge.method"], "S256")

    def test_realm_has_no_users_and_no_self_sign_up(self):
        realm = self.realm()
        self.assertNotIn("users", realm)
        self.assertIs(realm["registrationAllowed"], False)
        self.assertNotIn("password", json.dumps(realm).lower().replace("resetpasswordallowed", ""))

    def test_origins_must_be_https_without_a_path(self):
        for bad in ("http://chat.example.com", "https://chat.example.com/", "https://chat.example.com/app",
                    "https://localhost", "chat.example.com"):
            with self.subTest(origin=bad), self.assertRaises(ValueError):
                identity.identity("node-a", PUBLIC, bad)
            with self.subTest(public=bad), self.assertRaises(ValueError):
                identity.identity("node-a", bad, APP)


class ServerTests(unittest.TestCase):
    def test_image_is_the_pinned_multi_arch_index(self):
        self.assertEqual(container(rendered())["image"], identity.IMAGE)
        self.assertRegex(identity.IMAGE, r"^quay\.io/keycloak/keycloak:26\.0\.7@sha256:[0-9a-f]{64}$")

    def test_admin_credentials_come_only_from_the_operator_secret(self):
        env = {e["name"]: e for e in container(rendered())["env"]}
        for name, key in (("KC_BOOTSTRAP_ADMIN_USERNAME", "username"), ("KC_BOOTSTRAP_ADMIN_PASSWORD", "password")):
            with self.subTest(name=name):
                self.assertNotIn("value", env[name])
                self.assertEqual(env[name]["valueFrom"]["secretKeyRef"], {"name": "target-identity-admin", "key": key})

    def test_urls_come_from_the_public_hostname_behind_the_edge(self):
        env = {e["name"]: e.get("value") for e in container(rendered())["env"]}
        self.assertEqual(env["KC_HOSTNAME"], PUBLIC)
        self.assertEqual(env["KC_PROXY_HEADERS"], "xforwarded")
        self.assertEqual(env["KC_HTTP_ENABLED"], "true")
        self.assertEqual((env["KC_DB"], env["KC_CACHE"]), ("dev-file", "local"))

    def test_runs_unprivileged_on_its_own_tokenless_account(self):
        pod = rendered()["Deployment"]["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "target-identity")
        self.assertIn("target-identity", render.ACCOUNTS)
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertIs(pod["securityContext"]["runAsNonRoot"], True)
        self.assertEqual(container(rendered())["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertIs(container(rendered())["securityContext"]["allowPrivilegeEscalation"], False)

    def test_store_is_a_retained_local_volume_of_its_own(self):
        volume = rendered()["PersistentVolume"]
        self.assertEqual(volume["spec"]["local"]["path"], "/var/lib/bravo-target-dev/identity")
        self.assertEqual(volume["spec"]["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(volume["spec"]["claimRef"], {"namespace": "target-dev", "name": "target-identity"})
        mounts = {m["name"]: m["mountPath"] for m in container(rendered())["volumeMounts"]}
        self.assertEqual(mounts["data"], "/opt/keycloak/data/h2")

    def test_only_the_edge_tunnel_reaches_it_and_it_calls_nothing(self):
        policy = rendered()["NetworkPolicy"]["spec"]
        self.assertEqual(policy["ingress"], [{"from": [{"podSelector": {"matchLabels": {"app": "bravo-edge-tunnel"}}}],
                                              "ports": [{"protocol": "TCP", "port": 8080}]}])
        self.assertEqual(policy["egress"], [])
        self.assertEqual(set(policy["policyTypes"]), {"Ingress", "Egress"})

    def test_cli_renders_a_list(self):
        result = subprocess.run([sys.executable, str(WORKLOADS / "identity.py"), "--node-name", "node-a",
                                 "--public-url", PUBLIC, "--app-origin", APP],
                                capture_output=True, text=True, check=True, cwd=WORKLOADS)
        self.assertEqual(json.loads(result.stdout)["kind"], "List")


def cognito_user(username, status="CONFIRMED", enabled=True, **attributes):
    attributes = {"sub": f"sub-{username}", **attributes}
    return {"Username": username, "Enabled": enabled, "UserStatus": status,
            "Attributes": [{"Name": k, "Value": v} for k, v in attributes.items()]}


class UserMoveTests(unittest.TestCase):
    """Cognito exports no password hashes: accounts move, passwords are set again on first sign-in."""

    def test_users_move_without_passwords_and_must_set_one(self):
        result, skipped = identity_users.convert({"Users": [
            cognito_user("Visitor01", email="v@example.test", email_verified="true", given_name="Vi"),
        ]})
        (user,) = result["users"]
        self.assertEqual(skipped, [])
        self.assertEqual(user["username"], "visitor01")
        self.assertEqual(user["requiredActions"], ["UPDATE_PASSWORD"])
        self.assertNotIn("credentials", user)
        self.assertEqual((user["email"], user["emailVerified"], user["firstName"]), ("v@example.test", True, "Vi"))
        self.assertEqual(user["attributes"], {"cognito_sub": ["sub-Visitor01"]})
        self.assertEqual(result["ifResourceExists"], "SKIP")

    def test_disabled_or_unconfirmed_accounts_stay_behind(self):
        result, skipped = identity_users.convert({"Users": [
            cognito_user("off", enabled=False), cognito_user("pending", status="UNCONFIRMED"),
            cognito_user("kept", status="FORCE_CHANGE_PASSWORD"),
        ]})
        self.assertEqual([u["username"] for u in result["users"]], ["kept"])
        self.assertEqual([s["username"] for s in skipped], ["off", "pending"])

    def test_case_collisions_stop_the_move(self):
        with self.assertRaises(ValueError):
            identity_users.convert({"Users": [cognito_user("Kim"), cognito_user("kim")]})

    def test_the_moved_name_is_the_author_name_on_both_issuers(self):
        # demo-chat names authors by preferred_username, then cognito:username (demo-app #28).
        result, _ = identity_users.convert({"Users": [cognito_user("visitor01")]})
        self.assertEqual(result["users"][0]["username"], "visitor01")


@unittest.skipUnless(os.getenv("BRAVO_TEST_KEYCLOAK") and shutil.which("docker"), "set BRAVO_TEST_KEYCLOAK=1 with docker")
class RenderedServerStartsTests(unittest.TestCase):
    """Start the rendered image with the rendered env and realm; local http stands in for the Edge."""

    def test_rendered_server_serves_the_issuer_demo_chat_expects(self):
        public = "http://127.0.0.1:18183"
        items = {item["kind"]: item for item in identity.identity("node-a", "https://login.example.com", APP)}
        env = {e["name"]: e.get("value") for e in container(items)["env"] if "value" in e}
        env["KC_HOSTNAME"] = public  # the container test has no Edge; the rendered value is checked above
        name = f"bravo-identity-test-{os.getpid()}"
        with tempfile.TemporaryDirectory() as tmp:
            imports, data = Path(tmp, "import"), Path(tmp, "h2")
            imports.mkdir()
            data.mkdir()
            data.chmod(0o777)
            for file, text in items["ConfigMap"]["data"].items():
                Path(imports, file).write_text(text)
            args = ["docker", "run", "-d", "--name", name, "--user", "1000:1000", "-p", "127.0.0.1:18183:8080",
                    "-p", "127.0.0.1:19183:9000", "-v", f"{imports}:/opt/keycloak/data/import:ro",
                    "-v", f"{data}:/opt/keycloak/data/h2",
                    "-e", "KC_BOOTSTRAP_ADMIN_USERNAME=admin", "-e", "KC_BOOTSTRAP_ADMIN_PASSWORD=container-test-only"]
            for key, value in env.items():
                args += ["-e", f"{key}={value}"]
            subprocess.run([*args, identity.IMAGE, *container(items)["args"]], check=True, capture_output=True)
            try:
                deadline = time.monotonic() + 180
                while True:
                    try:
                        with urllib.request.urlopen("http://127.0.0.1:19183/health/ready", timeout=3) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        pass
                    self.assertLess(time.monotonic(), deadline, "Keycloak did not become ready")
                    time.sleep(3)
                url = f"{public}/realms/bravo/.well-known/openid-configuration"
                with urllib.request.urlopen(url, timeout=5) as response:
                    document = json.load(response)
                self.assertEqual(document["issuer"], f"{public}/realms/bravo")
                self.assertIn("S256", document["code_challenge_methods_supported"])
                with urllib.request.urlopen(document["jwks_uri"], timeout=5) as response:
                    self.assertTrue(any(k["kty"] == "RSA" for k in json.load(response)["keys"]))
            finally:
                subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)


if __name__ == "__main__":
    unittest.main()
