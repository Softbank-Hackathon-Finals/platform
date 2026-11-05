"""Personal profiles: portable authentication, bounded RBAC and identity failures."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra/envs/rehearsal-access/personal.py"
SPEC = importlib.util.spec_from_file_location("personal_access", SCRIPT)
personal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(personal)


def profile():
    return {
        "apiVersion": personal.VERSION,
        "kind": "PersonalAccessProfile",
        "name": "member-lab",
        "cluster": {
            "server": "https://cluster.example.test:443",
            "inClusterServer": "https://192.0.2.10:6443",
            "uid": "abcdefff-abcd-4abc-8abc-abcdefabcdef",
            "trust": {"mode": "system"},
        },
        "auth": {"mode": "tailscale-proxy"},
        "username": "member@example.test",
        "namespace": {
            "name": "sandbox-member",
            "uid": "bcdefaaa-bcde-4bcd-8bcd-bcdefabcdefa",
        },
        "permissions": {
            "namespaceRules": [
                {
                    "apiGroup": "",
                    "resources": ["pods", "configmaps"],
                    "verbs": personal.WRITE.copy(),
                }
            ],
            "podLogs": True,
            "podExec": True,
            "readOnlyNamespaces": [
                {
                    "name": "target-app",
                    "rules": [
                        {
                            "apiGroup": "",
                            "resources": ["pods"],
                            "verbs": personal.READ.copy(),
                        }
                    ],
                }
            ],
        },
    }


def namespace(name, uid):
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "uid": uid,
            "labels": {"pod-security.kubernetes.io/enforce": "restricted"},
        },
    }


def allows(objects, ns, verb, resource, name=None, group=""):
    # Deliberately evaluates namespace and resource-name scope, rather than comparing render text.
    return any(
        obj["kind"] in ("Role", "ClusterRole")
        and (obj["kind"] == "ClusterRole" or obj["metadata"]["namespace"] == ns)
        and any(
            group in r["apiGroups"]
            and resource in r["resources"]
            and verb in r["verbs"]
            and ("resourceNames" not in r or name in r["resourceNames"])
            for r in obj["rules"]
        )
        for obj in objects
    )


class PersonalAccessTests(unittest.TestCase):
    def test_proxy_can_forward_only_configured_human_identity(self):
        for account, ns, username in [
            ("api-proxy", "network-access", "member@example.test"),
            ("other-operator", "other-tailnet", "other@example.test"),
        ]:
            with self.subTest(proxy=account):
                p = profile()
                p["username"] = username
                p["auth"]["proxyServiceAccount"] = {"name": account, "namespace": ns}
                objects = personal.rbac(p)["items"]
                proxy = {"kind": "ServiceAccount", "name": account, "namespace": ns}
                bindings = [
                    o
                    for o in objects
                    if o["kind"].endswith("Binding") and proxy in o["subjects"]
                ]
                self.assertEqual(len(bindings), 1)
                binding = bindings[0]
                self.assertEqual(binding["subjects"], [proxy])
                roles = [
                    o
                    for o in objects
                    if o["kind"] == binding["roleRef"]["kind"]
                    and o["metadata"]["name"] == binding["roleRef"]["name"]
                ]
                self.assertEqual(len(roles), 1)
                self.assertTrue(allows(roles, None, "impersonate", "users", username))
                for resource, target in [
                    ("users", "stranger@example.test"),
                    ("users", "system:admin"),
                    ("groups", "system:masters"),
                    ("serviceaccounts", "default"),
                ]:
                    self.assertFalse(
                        allows(roles, None, "impersonate", resource, target)
                    )
                self.assertFalse(allows(roles, "sandbox-member", "create", "pods"))
                human = {"kind": "User", "apiGroup": personal.RBAC, "name": username}
                self.assertTrue(all(human not in o["subjects"] for o in bindings))
                self.assertEqual(personal.kubeconfig(p)["users"][0]["user"], {})

    def test_proxy_config_is_optional_and_restricted_to_proxy_mode(self):
        p = profile()
        self.assertFalse(
            allows(
                personal.rbac(p)["items"], None, "impersonate", "users", p["username"]
            )
        )
        for proxy in [
            {"name": "*", "namespace": "network-access"},
            {"name": "api-proxy"},
            {"name": "api-proxy", "namespace": "../other"},
            {"name": "api-proxy", "namespace": "network-access", "groups": ["*"]},
        ]:
            p["auth"]["proxyServiceAccount"] = proxy
            with self.subTest(proxy=proxy), self.assertRaises(ValueError):
                personal.rbac(p)
        for auth in [
            {
                "mode": "client-certificate",
                "certificateFile": "/tmp/member.crt",
                "keyFile": "/tmp/member.key",
            },
            {
                "mode": "exec",
                "command": "/bin/credential",
                "args": [],
                "interactiveMode": "Never",
            },
        ]:
            p["auth"] = {
                **auth,
                "proxyServiceAccount": {
                    "name": "api-proxy",
                    "namespace": "network-access",
                },
            }
            with self.subTest(mode=auth["mode"]), self.assertRaises(ValueError):
                personal.rbac(p)

    def test_three_authentication_modes_and_two_os_path_formats(self):
        configurations = [
            ({"mode": "tailscale-proxy"}, {"mode": "system"}, {}),
            (
                {
                    "mode": "client-certificate",
                    "certificateFile": "/var/member/access/client.crt",
                    "keyFile": "/var/member/access/client.key",
                },
                {"mode": "ca-file", "caFile": "/var/member/access/ca.pem"},
                {"tlsServerName": "api.example.test"},
            ),
            (
                {
                    "mode": "exec",
                    "command": "C:\\Tools\\oidc-login.exe",
                    "args": ["get-token", "--issuer=https://id.example.test"],
                    "interactiveMode": "IfAvailable",
                },
                {"mode": "ca-file", "caFile": "C:\\Users\\member\\ca.pem"},
                {},
            ),
        ]
        for auth, trust, extra in configurations:
            with self.subTest(mode=auth["mode"]):
                p = profile()
                p["auth"] = auth
                p["cluster"]["trust"] = trust
                p["cluster"].update(extra)
                result = personal.kubeconfig(p)
                self.assertNotIn("insecure-skip-tls-verify", json.dumps(result))
                self.assertNotIn("client-key-data", json.dumps(result))
                self.assertEqual(
                    result["contexts"][0]["context"]["namespace"], "sandbox-member"
                )
                if auth["mode"] == "tailscale-proxy":
                    self.assertEqual(result["users"][0]["user"], {})
                elif auth["mode"] == "exec":
                    self.assertEqual(
                        result["users"][0]["user"]["exec"]["command"], auth["command"]
                    )
                else:
                    self.assertEqual(
                        result["users"][0]["user"]["client-key"], auth["keyFile"]
                    )

    def test_ipv6_nondefault_port_and_independent_profiles(self):
        first, second = profile(), profile()
        second["name"] = "other-env"
        second["namespace"]["name"] = "qa-other"
        second["username"] = "other@example.test"
        second["cluster"]["server"] = "https://[2001:db8::1]:7443"
        self.assertEqual(
            personal.kubeconfig(second)["clusters"][0]["cluster"]["server"],
            second["cluster"]["server"],
        )
        self.assertNotIn("sandbox-member", json.dumps(personal.rbac(second)))
        self.assertEqual(profile(), first)

    def test_authorization_matrix(self):
        items = personal.rbac(profile())["items"]
        for ns, verb, resource, name, expected in [
            ("sandbox-member", "create", "pods", None, True),
            ("sandbox-member", "create", "pods/exec", None, True),
            ("target-app", "list", "pods", None, True),
            ("target-app", "create", "pods", None, False),
            ("target-app", "get", "secrets", None, False),
            ("other", "create", "pods", None, False),
            (None, "get", "namespaces", "sandbox-member", True),
            (None, "get", "namespaces", "kube-system", True),
            (None, "get", "namespaces", "other", False),
            (None, "delete", "namespaces", "sandbox-member", False),
            (None, "create", "namespaces", None, False),
            (None, "list", "nodes", None, False),
            ("sandbox-member", "create", "serviceaccounts/token", None, False),
        ]:
            with self.subTest(ns=ns, verb=verb, resource=resource):
                self.assertEqual(allows(items, ns, verb, resource, name), expected)

    def test_runner_impersonates_exact_serviceaccount_in_exact_namespace(self):
        p = profile()
        p["testRunner"] = {
            "serviceAccount": "human-checker",
            "impersonateServiceAccount": "rehearsal-exec",
            "namespace": {
                "name": "access-control",
                "uid": "cdefabcd-abcd-4abc-8abc-abcdefabcdef",
            },
            "podName": "personal-checker",
            "apiEndpoints": [
                {"ip": "192.0.2.10", "port": 6443},
                {"ip": "2001:db8::10", "port": 443},
            ],
        }
        items = personal.rbac(p)["items"]
        self.assertTrue(
            allows(
                items,
                "sandbox-member",
                "impersonate",
                "serviceaccounts",
                "rehearsal-exec",
            )
        )
        self.assertFalse(
            allows(items, "other", "impersonate", "serviceaccounts", "rehearsal-exec")
        )
        self.assertFalse(
            allows(items, "sandbox-member", "impersonate", "serviceaccounts", "default")
        )
        self.assertFalse(allows(items, None, "impersonate", "users", "admin"))
        self.assertFalse(items[0]["automountServiceAccountToken"])
        self.assertEqual(items[0]["metadata"]["namespace"], "access-control")
        self.assertFalse(allows(items, "access-control", "create", "pods"))
        self.assertFalse(allows(items, "access-control", "get", "secrets"))
        self.assertTrue(
            allows(items, "access-control", "create", "pods/exec", "personal-checker")
        )
        self.assertFalse(
            allows(items, "access-control", "create", "pods/exec", "another")
        )
        manager_bindings = [
            i
            for i in items
            if i["kind"] == "RoleBinding"
            and i["metadata"]["namespace"] == "access-control"
        ]
        self.assertEqual([x["kind"] for x in manager_bindings[0]["subjects"]], ["User"])
        pod = personal.probe(
            p,
            "registry.example.test/kubectl@sha256:" + "a" * 64,
            "human-checker",
            "personal-checker",
        )
        self.assertEqual(pod["metadata"]["namespace"], "access-control")
        net = personal.network(p)["items"]
        self.assertEqual(net[0]["spec"]["egress"], [])
        self.assertEqual(
            net[1]["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"], "192.0.2.10/32"
        )
        self.assertEqual(
            net[1]["spec"]["egress"][1]["to"][0]["ipBlock"]["cidr"], "2001:db8::10/128"
        )
        self.assertEqual(
            net[1]["spec"]["podSelector"]["matchLabels"], pod["metadata"]["labels"]
        )
        c = namespace("kube-system", p["cluster"]["uid"])
        n = namespace(p["namespace"]["name"], p["namespace"]["uid"])
        manager = namespace("access-control", p["testRunner"]["namespace"]["uid"])
        with self.assertRaises(ValueError):
            personal.verify(p, c, n)
        self.assertFalse(
            personal.verify(p, c, n, runner_namespace=manager)["personalLoginVerified"]
        )
        manager["metadata"]["uid"] = n["metadata"]["uid"]
        with self.assertRaises(ValueError):
            personal.verify(p, c, n, runner_namespace=manager)
        p["testRunner"]["apiEndpoints"][0]["ip"] = "0.0.0.0/0"
        with self.assertRaises(ValueError):
            personal.network(p)
        p["testRunner"]["apiEndpoints"][0]["ip"] = "192.0.2.10"
        p["testRunner"]["namespace"]["name"] = p["namespace"]["name"]
        with self.assertRaises(ValueError):
            personal.rbac(p)

    def test_rejects_unsafe_and_ambiguous_configurations(self):
        mutators = [
            lambda p: p.update(apiVersion="v99"),
            lambda p: p.update(username="system:masters"),
            lambda p: p.update(unrecognized=True),
            lambda p: p["auth"].update(token="private-marker"),
            lambda p: p["cluster"].update(server="http://api.example.test"),
            lambda p: p["cluster"].update(
                server="https://user:private-marker@api.example.test"
            ),
            lambda p: p["cluster"].update(
                server="https://api.example.test/?token=private-marker"
            ),
            lambda p: p["cluster"].update(trust={"mode": "insecure"}),
            lambda p: p["namespace"].update(name="kube-system"),
            lambda p: p["namespace"].update(uid="not-a-uid"),
            lambda p: p["permissions"]["namespaceRules"][0].update(resources=["*"]),
            lambda p: p["permissions"]["namespaceRules"][0].update(resources=["nodes"]),
            lambda p: p["permissions"]["namespaceRules"][0].update(
                resources=["serviceaccounts/token"]
            ),
            lambda p: p["permissions"]["namespaceRules"][0].update(verbs=["bind"]),
            lambda p: p["permissions"]["namespaceRules"][0].update(verbs=["escalate"]),
            lambda p: p["permissions"]["namespaceRules"][0].update(
                verbs=["deletecollection"]
            ),
            lambda p: p["permissions"]["readOnlyNamespaces"][0]["rules"][0].update(
                resources=["secrets"]
            ),
            lambda p: p["permissions"]["readOnlyNamespaces"][0]["rules"][0].update(
                verbs=["patch"]
            ),
            lambda p: p["permissions"]["readOnlyNamespaces"].append(
                copy.deepcopy(p["permissions"]["readOnlyNamespaces"][0])
            ),
        ]
        for i, mutate in enumerate(mutators):
            p = profile()
            mutate(p)
            with self.subTest(i=i), self.assertRaises(ValueError):
                personal.rbac(p)

    def test_verify_readbacks_distinguishes_admin_preflight_from_personal_login(self):
        p = profile()
        c = namespace("kube-system", p["cluster"]["uid"])
        n = namespace(p["namespace"]["name"], p["namespace"]["uid"])
        self.assertFalse(personal.verify(p, c, n)["personalLoginVerified"])
        me = {
            "kind": "SelfSubjectReview",
            "status": {
                "userInfo": {
                    "username": p["username"],
                    "groups": ["system:authenticated"],
                }
            },
        }
        self.assertTrue(personal.verify(p, c, n, me)["personalLoginVerified"])
        for changed in (
            "cluster",
            "namespace",
            "terminating",
            "psa",
            "username",
            "admin",
        ):
            cc, nn, mm = copy.deepcopy((c, n, me))
            if changed == "cluster":
                cc["metadata"]["uid"] = n["metadata"]["uid"]
            if changed == "namespace":
                nn["metadata"]["uid"] = c["metadata"]["uid"]
            if changed == "terminating":
                nn["metadata"]["deletionTimestamp"] = "2026-11-05T00:00:00Z"
            if changed == "psa":
                nn["metadata"]["labels"] = {}
            if changed == "username":
                mm["status"]["userInfo"]["username"] = "another@example.test"
            if changed == "admin":
                mm["status"]["userInfo"]["groups"] = ["system:masters"]
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                personal.verify(p, cc, nn, mm)

    def test_probe_is_digest_pinned_restricted_and_bounded(self):
        pod = personal.probe(
            profile(),
            "registry.example.test/kubectl@sha256:" + "a" * 64,
            "rehearsal-exec",
            "identity-probe",
        )
        spec = pod["spec"]
        self.assertEqual(spec["serviceAccountName"], "rehearsal-exec")
        config = json.loads(
            pod["metadata"]["annotations"]["bravo.dev/in-cluster-config"]
        )
        self.assertEqual(
            config["clusters"][0]["cluster"]["server"], "https://192.0.2.10:6443"
        )
        self.assertIn("tokenFile", config["users"][0]["user"])
        self.assertNotIn("token", config["users"][0]["user"])
        self.assertEqual(spec["containers"][0]["env"][0]["name"], "KUBECONFIG")
        self.assertEqual(spec["activeDeadlineSeconds"], 1800)
        self.assertFalse(
            spec["containers"][0]["securityContext"]["allowPrivilegeEscalation"]
        )
        with self.assertRaises(ValueError):
            personal.probe(
                profile(), "kubectl:latest", "rehearsal-exec", "identity-probe"
            )

    def test_cli_rejects_without_echoing_private_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            p = profile()
            p["auth"]["token"] = "private-marker"
            path.write_text(json.dumps(p))
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "rbac", "--profile", str(path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("private-marker", result.stderr)


if __name__ == "__main__":
    unittest.main()
