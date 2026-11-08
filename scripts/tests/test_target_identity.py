"""T30 target sign-in server: Keycloak replaces the source Cognito user pool by a settings change.

The render tests pin what the browser and demo-chat rely on. The container test (BRAVO_TEST_KEYCLOAK=1)
starts the rendered image with the rendered settings and realm, and asks it for the issuer demo-chat expects.
"""
import base64
import hashlib
import html
import http.cookiejar
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
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

    def test_load_generator_client_is_separate_and_outside_the_import(self):
        # The import keeps only the browser client; the generator's client is registered later (README).
        self.assertEqual([c["clientId"] for c in self.realm()["clients"]], ["demo-chat"])
        client = identity.loadgen_client()
        self.assertEqual(client["clientId"], "demo-chat-loadgen")
        self.assertIs(client["publicClient"], False)
        self.assertEqual(client["clientAuthenticatorType"], "client-secret")
        self.assertNotIn("secret", client)  # Keycloak makes it; nothing rendered holds it
        self.assertIs(client["directAccessGrantsEnabled"], True)
        for flag in ("standardFlowEnabled", "implicitFlowEnabled", "serviceAccountsEnabled", "fullScopeAllowed"):
            with self.subTest(flag=flag):
                self.assertIs(client[flag], False)
        self.assertEqual((client["redirectUris"], client["webOrigins"]), ([], []))
        # Only the scopes behind sub and preferred_username. Given either list, Keycloak drops both realm lists,
        # so an empty optional list alone loses preferred_username (container test). The optional list is
        # pinned here: with the default list written out, the server already leaves offline_access out.
        self.assertEqual(client["defaultClientScopes"], ["basic", "profile"])
        self.assertEqual(client["optionalClientScopes"], [])  # no offline_access
        self.assertEqual(client["attributes"], {"use.refresh.tokens": "false"})

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
                self.assertEqual(env[name]["valueFrom"]["secretKeyRef"], {"name": "target-login-admin", "key": key})

    def test_urls_come_from_the_public_hostname_behind_the_edge(self):
        env = {e["name"]: e.get("value") for e in container(rendered())["env"]}
        self.assertEqual(env["KC_HOSTNAME"], PUBLIC)
        self.assertEqual(env["KC_PROXY_HEADERS"], "xforwarded")
        self.assertEqual(env["KC_HTTP_ENABLED"], "true")
        self.assertEqual((env["KC_DB"], env["KC_CACHE"]), ("dev-file", "local"))

    def test_runs_unprivileged_on_its_own_tokenless_account(self):
        pod = rendered()["Deployment"]["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "target-login")
        self.assertIn("target-login", render.ACCOUNTS)
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertIs(pod["securityContext"]["runAsNonRoot"], True)
        self.assertEqual(container(rendered())["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertIs(container(rendered())["securityContext"]["allowPrivilegeEscalation"], False)

    def test_store_is_a_retained_local_volume_of_its_own(self):
        volume = rendered()["PersistentVolume"]
        self.assertEqual(volume["spec"]["local"]["path"], "/var/lib/bravo-target-dev/login")
        self.assertEqual(volume["spec"]["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(volume["spec"]["claimRef"], {"namespace": "target-dev", "name": "target-login"})
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
    """Cognito exports no password hashes: accounts move without one and get a temporary password after."""

    def test_users_move_without_credentials_and_must_change_the_temporary_one(self):
        result, skipped = identity_users.convert({"Users": [
            cognito_user("visitor01", email="v@example.test", email_verified="true", given_name="Vi"),
        ]})
        (user,) = result["users"]
        self.assertEqual(skipped, [])
        self.assertEqual(user["username"], "visitor01")
        self.assertEqual(user["requiredActions"], ["UPDATE_PASSWORD"])
        self.assertNotIn("credentials", user)
        self.assertEqual((user["email"], user["emailVerified"], user["firstName"]), ("v@example.test", True, "Vi"))
        self.assertEqual(user["attributes"], {"cognito_sub": ["sub-visitor01"]})
        self.assertEqual(result["ifResourceExists"], "SKIP")

    def test_disabled_or_unconfirmed_accounts_stay_behind(self):
        result, skipped = identity_users.convert({"Users": [
            cognito_user("off", enabled=False), cognito_user("pending", status="UNCONFIRMED"),
            cognito_user("kept", status="FORCE_CHANGE_PASSWORD"),
        ]})
        self.assertEqual([u["username"] for u in result["users"]], ["kept"])
        self.assertEqual([s["username"] for s in skipped], ["off", "pending"])

    def test_upper_case_names_are_refused_not_folded(self):
        # demo-chat does not fold case: "Visitor01" and "visitor01" would be two authors.
        result, skipped = identity_users.convert({"Users": [cognito_user("Visitor01"), cognito_user("kim")]})
        self.assertEqual([u["username"] for u in result["users"]], ["kim"])
        self.assertEqual(skipped, [{"username": "Visitor01", "reason": "upper-case name: would become another author"}])


class EdgeLikeCookies(http.cookiejar.DefaultCookiePolicy):
    """Keycloak marks its session cookies Secure; the Edge serves https, this local test plain http."""

    def return_ok_secure(self, cookie, request):
        return True


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def claims(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


@unittest.skipUnless(os.getenv("BRAVO_TEST_KEYCLOAK") and shutil.which("docker"), "set BRAVO_TEST_KEYCLOAK=1 with docker")
class RenderedServerTests(unittest.TestCase):
    """Start the rendered image with the rendered env and realm; local http stands in for the Edge."""

    PUBLIC = "http://127.0.0.1:18183"
    ADMIN = ("admin", "container-test-only")

    @classmethod
    def setUpClass(cls):
        items = {item["kind"]: item for item in identity.identity("node-a", "https://login.example.com", APP)}
        cls.args = container(items)["args"]
        env = {e["name"]: e.get("value") for e in container(items)["env"] if "value" in e}
        env["KC_HOSTNAME"] = cls.PUBLIC  # the container test has no Edge; the rendered value is checked above
        cls.name = f"bravo-login-test-{os.getpid()}"
        cls.tmp = tempfile.TemporaryDirectory()
        imports, data = Path(cls.tmp.name, "import"), Path(cls.tmp.name, "h2")
        imports.mkdir()
        data.mkdir()
        data.chmod(0o777)
        for file, text in items["ConfigMap"]["data"].items():
            Path(imports, file).write_text(text)
        args = ["docker", "run", "-d", "--name", cls.name, "--user", "1000:1000", "-p", "127.0.0.1:18183:8080",
                "-p", "127.0.0.1:19183:9000", "-v", f"{imports}:/opt/keycloak/data/import:ro",
                "-v", f"{data}:/opt/keycloak/data/h2",
                "-e", f"KC_BOOTSTRAP_ADMIN_USERNAME={cls.ADMIN[0]}", "-e", f"KC_BOOTSTRAP_ADMIN_PASSWORD={cls.ADMIN[1]}"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        subprocess.run([*args, identity.IMAGE, *cls.args], check=True, capture_output=True)
        deadline = time.monotonic() + 180
        while True:
            try:
                with urllib.request.urlopen("http://127.0.0.1:19183/health/ready", timeout=3) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > deadline:
                cls.tearDownClass()
                raise AssertionError("Keycloak did not become ready")
            time.sleep(3)
        with urllib.request.urlopen(f"{cls.PUBLIC}/realms/bravo/.well-known/openid-configuration", timeout=5) as r:
            cls.document = json.load(r)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", cls.name], check=False, capture_output=True)
        cls.tmp.cleanup()

    def authorize(self, **overrides):
        params = {"client_id": "demo-chat", "response_type": "code", "scope": "openid", "redirect_uri": f"{APP}/",
                  "state": "s", "code_challenge": "x" * 43, "code_challenge_method": "S256", **overrides}
        return self.document["authorization_endpoint"] + "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None})

    def fetch(self, opener, url, data=None):
        """(status, Location, body) without following a redirect."""
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        try:
            with opener.open(urllib.request.Request(url, data=body), timeout=10) as response:
                return response.status, response.headers.get("Location"), response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Location"), error.read().decode()

    def admin(self, method, path, body=None):
        token_url = f"{self.PUBLIC}/realms/master/protocol/openid-connect/token"
        form = urllib.parse.urlencode({"grant_type": "password", "client_id": "admin-cli",
                                       "username": self.ADMIN[0], "password": self.ADMIN[1]}).encode()
        with urllib.request.urlopen(token_url, data=form, timeout=10) as response:
            token = json.load(response)["access_token"]
        request = urllib.request.Request(f"{self.PUBLIC}/admin/realms/bravo{path}", method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            text = response.read().decode()
            return json.loads(text) if text else None

    def test_issuer_and_keys_are_what_demo_chat_expects(self):
        self.assertEqual(self.document["issuer"], f"{self.PUBLIC}/realms/bravo")
        self.assertIn("S256", self.document["code_challenge_methods_supported"])
        with urllib.request.urlopen(self.document["jwks_uri"], timeout=5) as response:
            self.assertTrue(any(k["kty"] == "RSA" for k in json.load(response)["keys"]))

    def test_only_the_exact_callback_opens_the_login_page_and_sign_up_is_closed(self):
        opener = urllib.request.build_opener(NoRedirect)
        status, _, page = self.fetch(opener, self.authorize())
        self.assertEqual(status, 200)
        self.assertNotIn("login-actions/registration", page)
        for redirect in ("https://evil.example/", f"{APP}/other", APP):
            with self.subTest(redirect=redirect):
                self.assertEqual(self.fetch(opener, self.authorize(redirect_uri=redirect))[0], 400)
        registration = self.authorize().replace("/auth?", "/registrations?")
        self.assertIn(self.fetch(opener, registration)[0], (400, 403, 404))

    def test_pkce_is_required_and_only_s256(self):
        opener = urllib.request.build_opener(NoRedirect)
        for overrides in ({"code_challenge": None, "code_challenge_method": None}, {"code_challenge_method": "plain"}):
            with self.subTest(overrides=overrides):
                status, location, _ = self.fetch(opener, self.authorize(**overrides))
                self.assertEqual(status, 302)
                self.assertIn("error=invalid_request", location)

    def test_implicit_and_password_grants_are_refused(self):
        opener = urllib.request.build_opener(NoRedirect)
        status, location, _ = self.fetch(opener, self.authorize(response_type="token"))
        self.assertIn(status, (302, 400))
        self.assertTrue(location is None or "error=" in location)
        status, _, body = self.fetch(opener, self.document["token_endpoint"], {
            "grant_type": "password", "client_id": "demo-chat", "username": "nobody", "password": "x"})
        self.assertEqual((status, json.loads(body)["error"]), (400, "unauthorized_client"))

    def test_realm_keeps_names_fixed_and_logs_in_by_name_only(self):
        realm = self.admin("GET", "")
        self.assertIs(realm["editUsernameAllowed"], False)
        self.assertIs(realm["loginWithEmailAllowed"], False)
        self.assertIs(realm["registrationAllowed"], False)
        profile = self.admin("GET", "/users/profile")
        for attribute in profile["attributes"]:
            with self.subTest(attribute=attribute["name"]):
                self.assertEqual(attribute["permissions"]["edit"], ["admin"])
                self.assertFalse(attribute.get("required"))

    def test_only_the_load_generator_client_takes_a_password(self):
        """README "부하 생성기 클라이언트": admin API registers it, Keycloak makes the secret, the moved account
        gets a permanent password (it has no person to pass UPDATE_PASSWORD) and signs in."""
        self.admin("POST", "/clients", identity.loadgen_client())
        (client,) = self.admin("GET", "/clients?clientId=demo-chat-loadgen")
        secret = self.admin("GET", f"/clients/{client['id']}/client-secret")["value"]
        moved, _ = identity_users.convert({"Users": [cognito_user("loadgen")]})
        self.admin("POST", "/partialImport", moved)
        (user,) = self.admin("GET", "/users?exact=true&username=loadgen")
        self.assertEqual(user["requiredActions"], ["UPDATE_PASSWORD"])
        self.admin("PUT", f"/users/{user['id']}/reset-password",
                   {"type": "password", "value": "Load-Generator-1", "temporary": False})
        opener = urllib.request.build_opener(NoRedirect)

        def grant(client_id, **extra):
            status, _, body = self.fetch(opener, self.document["token_endpoint"], {
                "grant_type": "password", "scope": "openid", "client_id": client_id,
                "username": "loadgen", "password": "Load-Generator-1", **extra})
            return status, json.loads(body)

        # The browser client still refuses a password, even with the generator's client in the realm.
        status, body = grant("demo-chat")
        self.assertEqual((status, body["error"]), (400, "unauthorized_client"))
        # Keycloak answers a missing or wrong client secret with 401 unauthorized_client.
        for extra in ({}, {"client_secret": "wrong"}):
            with self.subTest(secret=bool(extra)):
                status, body = grant("demo-chat-loadgen", **extra)
                self.assertEqual((status, body["error"]), (401, "unauthorized_client"))
        status, body = grant("demo-chat-loadgen", client_secret=secret)
        self.assertEqual(status, 200)
        token = claims(body["id_token"])
        self.assertEqual((token["iss"], token["aud"], token["azp"], token["preferred_username"]),
                         (f"{self.PUBLIC}/realms/bravo", "demo-chat-loadgen", "demo-chat-loadgen", "loadgen"))
        # No refresh token and no offline one, which would outlive a password change and a user logout.
        self.assertNotIn("refresh_token", sorted(body))  # key names only: a failure must not print tokens
        status, offline = grant("demo-chat-loadgen", client_secret=secret, scope="openid offline_access")
        self.assertEqual((status, offline["error"]), (400, "invalid_scope"))
        self.assertEqual(self.admin("GET", f"/clients/{client['id']}/offline-session-count"), {"count": 0})
        # Keycloak does not bind this client to the loadgen account: any account's password yields a token
        # in that account's name. The boundary is the app, which takes this client's tokens only for the name
        # pinned in AUTH_ALSO_TRUST (demo-app #30). README "부하 생성기 클라이언트" states it.
        self.admin("POST", "/users", {"username": "visitor-lg", "enabled": True, "credentials": [
            {"type": "password", "value": "Visitor-Password-3", "temporary": False}]})
        status, other = self.fetch(opener, self.document["token_endpoint"], {
            "grant_type": "password", "scope": "openid", "client_id": "demo-chat-loadgen",
            "client_secret": secret, "username": "visitor-lg", "password": "Visitor-Password-3"})[::2]
        other_token = claims(json.loads(other)["id_token"])
        self.assertEqual((status, other_token["aud"], other_token["preferred_username"]),
                         (200, "demo-chat-loadgen", "visitor-lg"))
        # Its access token cannot reach the account console, where a user would edit their own profile.
        self.assertNotIn("account", claims(body["access_token"]).get("resource_access", {}))
        request = urllib.request.Request(f"{self.PUBLIC}/realms/bravo/account", headers={
            "Authorization": f"Bearer {body['access_token']}", "Accept": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=10)
        self.assertIn(refused.exception.code, (401, 403))
        # And nobody signs in through it in a browser.
        status, location, _ = self.fetch(opener, self.authorize(client_id="demo-chat-loadgen"))
        self.assertEqual(status, 400)
        self.assertIsNone(location)

    def test_cleanup_steps_each_end_the_load_generator_sign_in(self):
        """README "부하 생성기 클라이언트" 정리 · 회수. A copy of the client, so the test above keeps the real one."""
        name = "demo-chat-loadgen-cleanup"
        self.admin("POST", "/clients", {**identity.loadgen_client(), "clientId": name})
        (client,) = self.admin("GET", f"/clients?clientId={name}")
        path = f"/clients/{client['id']}"
        secret = self.admin("GET", f"{path}/client-secret")["value"]
        self.admin("POST", "/users", {"username": "loadgen-cleanup", "enabled": True, "credentials": [
            {"type": "password", "value": "Load-Cleanup-4", "temporary": False}]})
        (user,) = self.admin("GET", "/users?exact=true&username=loadgen-cleanup")
        opener = urllib.request.build_opener(NoRedirect)

        def grant(client_secret):
            status, _, body = self.fetch(opener, self.document["token_endpoint"], {
                "grant_type": "password", "scope": "openid", "client_id": name, "client_secret": client_secret,
                "username": "loadgen-cleanup", "password": "Load-Cleanup-4"})
            return status, json.loads(body).get("error")

        self.assertEqual(grant(secret)[0], 200)
        # Turned off, the client is refused. The README's partial PUT changes nothing else.
        before = self.admin("GET", path)
        self.admin("PUT", path, {"enabled": False})
        self.assertEqual(grant(secret), (401, "invalid_client"))
        after = self.admin("GET", path)
        self.assertEqual({**after, "enabled": True}, before)
        self.admin("PUT", path, {"enabled": True})
        # A new secret ends the old one.
        new = self.admin("POST", f"{path}/client-secret")["value"]
        self.assertEqual(grant(secret), (401, "unauthorized_client"))
        self.assertEqual(grant(new)[0], 200)
        # A disabled account gets nothing and keeps its name for the next measurement.
        self.admin("PUT", f"/users/{user['id']}", {"enabled": False})
        self.assertEqual(grant(new), (400, "invalid_grant"))
        self.assertEqual(self.admin("GET", f"/users/{user['id']}")["username"], "loadgen-cleanup")
        # A deleted client is unknown.
        self.admin("DELETE", path)
        self.assertEqual(grant(new), (401, "invalid_client"))

    def test_moved_account_needs_a_temporary_password_then_a_new_one(self):
        """README "사용자 옮기기": import → no sign-in at all → temporary password → new password → ID token."""
        partial, _ = identity_users.convert({"Users": [cognito_user("visitor01")]})
        self.admin("POST", "/partialImport", partial)
        (user,) = self.admin("GET", "/users?exact=true&username=visitor01")

        def sign_in(password, new_password=None):
            jar = http.cookiejar.CookieJar(policy=EdgeLikeCookies())
            opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(jar))
            verifier = b64url(os.urandom(32))
            challenge = b64url(hashlib.sha256(verifier.encode()).digest())
            status, _, page = self.fetch(opener, self.authorize(code_challenge=challenge))
            self.assertEqual(status, 200)
            form = (re.search(r'id="kc-form-login"[^>]*action="([^"]+)"', page)
                    or re.search(r'action="([^"]+)"[^>]*id="kc-form-login"', page))
            action = html.unescape(form.group(1))
            status, location, page = self.fetch(opener, action, {"username": "visitor01", "password": password})
            while status == 302 and location.startswith(self.PUBLIC):
                status, location, page = self.fetch(opener, location)
            if new_password is not None and status == 200 and "password-new" in page:
                action = html.unescape(re.search(r'action="([^"]+)"', page).group(1))
                status, location, page = self.fetch(opener, action, {
                    "password-new": new_password, "password-confirm": new_password})
            return status, location, page, verifier

        status, location, page, _ = sign_in("anything")
        # Back on the login form with Keycloak's wrong-credential answer, never on UPDATE_PASSWORD.
        self.assertIn(status, (200, 400))
        visible = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", page)))
        self.assertIn("Invalid username or password", visible, visible[:600])
        self.assertNotIn("password-new", page)
        self.admin("PUT", f"/users/{user['id']}/reset-password",
                   {"type": "password", "value": "Temporary-Only-1", "temporary": True})
        status, location, page, verifier = sign_in("Temporary-Only-1", new_password="Chosen-By-User-2")
        self.assertEqual(status, 302)
        self.assertTrue(location.startswith(f"{APP}/?"), location)  # no VERIFY_PROFILE stop on the way
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["code"][0]
        status, _, body = self.fetch(urllib.request.build_opener(NoRedirect), self.document["token_endpoint"], {
            "grant_type": "authorization_code", "client_id": "demo-chat", "code": code,
            "redirect_uri": f"{APP}/", "code_verifier": verifier})
        self.assertEqual(status, 200)
        token = claims(json.loads(body)["id_token"])
        self.assertEqual((token["aud"], token["preferred_username"]), ("demo-chat", "visitor01"))


if __name__ == "__main__":
    unittest.main()
