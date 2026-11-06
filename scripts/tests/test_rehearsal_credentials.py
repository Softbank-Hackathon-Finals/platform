"""The same registration renders installation objects and drives runtime acquisition."""

import base64
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "infra/envs/rehearsal-access"


def load(name):
    spec = importlib.util.spec_from_file_location(name, DIRECTORY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


credentials = load("credentials")
installation = load("credential_install")
UID = "00000000-0000-4000-8000-00000000000a"
OTHER = "00000000-0000-4000-8000-00000000000b"
ANCHOR = "00000000-0000-4000-8000-00000000000c"
NOW = datetime(2026, 11, 6, tzinfo=UTC)


def registration():
    return credentials.Registration(
        UID, "bravo-rehearsal-control", "t15-runner", OTHER, "tag:registered-workload",
        "https://kubernetes.default.svc.cluster.local",
        credentials.Endpoint("https://issuer.ns.svc.cluster.local", "issuer.example.ts.net"),
        credentials.Endpoint("https://target.ns.svc.cluster.local", "target.example.ts.net"))


class API:
    def __init__(self, fault=None):
        self.r, self.calls, self.fault, self.issued = registration(), [], fault, 0

    def __call__(self, endpoint, method, path, body=None, **kwargs):
        self.calls.append((endpoint, method, path, copy.deepcopy(body), kwargs))
        if path == credentials.SSR:
            if endpoint == self.r.issuer:
                return {"status": {"userInfo": {"groups": [] if self.fault == "issuer" else [self.r.issuer_group]}}}
            return {"status": {"userInfo": {
                "username": "proxy-user" if self.fault == "bearer" else self.r.subject, "uid": OTHER}}}
        if path.endswith("/kube-system"):
            return {"metadata": {"uid": OTHER if self.fault == "cluster" else UID}}
        if path.endswith("/token"):
            self.issued += 1
            if self.fault == "lost_response":
                raise credentials.APIError("credential API transport or response failed")
            seconds = 3600 if self.fault == "long_lifetime" else 600
            return {"status": {"token": "opaque-fixture-" + str(self.issued),
                               "expirationTimestamp": (NOW + timedelta(seconds=seconds)).isoformat()}}
        if "/secrets/" in path:
            if self.fault == "revoked":
                raise credentials.APIError("credential API returned HTTP 404")
            return {"metadata": {"uid": UID if self.fault == "anchor_replaced" else ANCHOR}}
        return {"metadata": {"uid": UID if self.fault == "account_replaced" else OTHER}}


class CredentialsTests(unittest.TestCase):
    def test_registration_roundtrips_without_another_schema_or_key(self):
        r = registration()
        self.assertEqual(credentials.Registration.parse(json.loads(json.dumps(asdict(r)))), r)
        with self.assertRaises(TypeError):
            credentials.Registration.parse(asdict(r) | {"secret": "unsupported"})
        with self.assertRaises(TypeError):
            credentials.Registration.parse({k: v for k, v in asdict(r).items() if k != "audience"})

    def test_names_are_bounded_and_stable_for_valid_large_account_names(self):
        r = registration()
        large = replace(r, service_account=".".join(["a" * 63] * 3 + ["b" * 61]))
        self.assertEqual(len(large.service_account), 253)
        self.assertEqual(len(large.name), len(r.name))
        self.assertNotEqual(large.name, r.name)
        self.assertNotEqual(replace(r, cluster_uid=ANCHOR).name, r.name)
        self.assertEqual(replace(r, issuer=replace(r.issuer, server="https://new.ns.svc.cluster.local")).name, r.name)

    def test_malformed_inputs_fail_before_any_api(self):
        r = registration()
        for key, value in [("namespace", "../other"), ("namespace", "한글"),
                           ("namespace", "x" * 64), ("service_account", "a/../b"),
                           ("issuer_group", "system:masters"), ("cluster_uid", "-" * 36),
                           ("audience", ""), ("target", r.issuer)]:
            with self.subTest(key=key, value=value), self.assertRaises((ValueError, TypeError)):
                replace(r, **{key: value})

    def test_endpoint_rejects_insecure_or_redirectable_configuration(self):
        for server in ("http://issuer", "https://user:pass@issuer", "https://issuer/other",
                       "https://issuer?token=x", "https://issuer:6443", "https://issuer#fragment"):
            with self.subTest(server=server), self.assertRaises(ValueError):
                credentials.Endpoint(server, "issuer.example.ts.net")

    def test_service_connection_keeps_verified_tls_and_http_authority(self):
        endpoint = registration().issuer
        connection, context, raw = Mock(), Mock(), Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{"kind":"SelfSubjectReview"}'
        with patch.object(credentials.http.client, "HTTPSConnection", return_value=connection), \
                patch.object(credentials.ssl, "create_default_context", return_value=context), \
                patch.object(credentials.socket, "create_connection", return_value=raw):
            credentials.request(endpoint, "POST", credentials.SSR, {})
        context.wrap_socket.assert_called_once_with(raw, server_hostname=endpoint.tls_server_name)
        self.assertEqual(connection.request.call_args.kwargs["headers"]["Host"], endpoint.tls_server_name)
        connection.close.assert_called_once()

    def test_restarts_acquire_fresh_credentials_with_the_same_registered_binding(self):
        api = API()
        first = credentials.acquire(api.r, ANCHOR, call=api, now=NOW)
        second = credentials.acquire(api.r, ANCHOR, call=api, now=NOW)
        self.assertNotEqual(first["status"]["token"], second["status"]["token"])
        self.assertEqual(first["status"]["expirationTimestamp"], (NOW + timedelta(seconds=540)).isoformat())
        requests = [call[3] for call in api.calls if call[2].endswith("/token")]
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0]["spec"]["boundObjectRef"]["uid"], ANCHOR)

    def test_wrong_registration_or_revocation_stops_before_token_issue(self):
        for fault in ("issuer", "cluster", "account_replaced", "anchor_replaced", "revoked"):
            with self.subTest(fault=fault), self.assertRaises(credentials.APIError):
                api = API(fault)
                try:
                    credentials.acquire(api.r, ANCHOR, call=api, now=NOW)
                finally:
                    self.assertEqual(api.issued, 0)

    def test_unexpected_lifetime_or_identity_never_reaches_consumer(self):
        for fault in ("long_lifetime", "bearer", "lost_response"):
            with self.subTest(fault=fault), self.assertRaises(credentials.APIError):
                api = API(fault)
                try:
                    credentials.acquire(api.r, ANCHOR, call=api, now=NOW)
                finally:
                    self.assertEqual(api.issued, 1)

    def test_kubeconfig_uses_standard_noninteractive_exec_without_token_material(self):
        config = credentials.kubeconfig(registration(), "/config/registration.json", "/app/credentials.py")
        auth = config["users"][0]["user"]
        self.assertEqual(set(auth), {"exec"})
        self.assertEqual(auth["exec"]["interactiveMode"], "Never")
        self.assertEqual(config["clusters"][0]["cluster"]["tls-server-name"], "target.example.ts.net")

    def test_issuer_grant_cannot_create_workloads_or_mint_another_account(self):
        r = registration()
        objects = installation.render(r)["items"]
        role = next(o for o in objects if o["kind"] == "Role")
        self.assertEqual(role["rules"][0]["resourceNames"], [r.service_account])
        self.assertEqual({v for rule in role["rules"] for v in rule["verbs"]}, {"get", "create"})
        self.assertEqual({res for rule in role["rules"] for res in rule["resources"]},
                         {"serviceaccounts/token", "serviceaccounts", "secrets"})
        anchor = next(o for o in objects if o["kind"] == "Secret")
        self.assertTrue(anchor["immutable"])
        self.assertNotIn("data", anchor)

    def test_issuer_policy_requires_short_lifetime_audience_and_revocation_binding(self):
        objects = installation.render(registration())["items"]
        self.assertEqual([o["kind"] for o in objects[:2]],
                         ["ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"])
        policy = next(o for o in objects if o["kind"] == "ValidatingAdmissionPolicy")
        expression = policy["spec"]["validations"][0]["expression"]
        for part in ("audiences", "expirationSeconds", "boundObjectRef.kind", "boundObjectRef.name", "boundObjectRef.uid"):
            self.assertIn(part, expression)
        self.assertEqual(policy["spec"]["failurePolicy"], "Fail")

    def test_issuer_route_reuses_observed_environment_without_changing_bearer_route(self):
        r = registration()
        observed = {"apiVersion": "tailscale.com/v1alpha1", "status": {"url": "https://target.example.ts.net"},
                    "spec": {"type": "kube-apiserver", "tailnet": "registered", "tags": ["tag:api"],
                             "proxyClass": "pinned-class", "kubeAPIServer": {"mode": "noauth"}}}
        original = copy.deepcopy(observed)
        obj = installation.issuer_proxy(r, observed)
        self.assertEqual(obj["spec"]["tailnet"], "registered")
        self.assertEqual(obj["spec"]["kubeAPIServer"]["mode"], "auth")
        self.assertEqual(observed, original)
        fragment = installation.issuer_tailnet_fragment(r, observed)
        self.assertEqual(fragment, {"autoApprovers": {"services": {"svc:issuer": ["tag:api"]}}})

    def test_certificate_bootstrap_is_conditional_and_preserves_provider_config(self):
        r = registration()
        proxy = {"metadata": {"name": r.name + "-issuer", "uid": OTHER},
                 "spec": {"kubeAPIServer": {"hostname": "issuer", "mode": "auth"}}}
        original = {"AuthKey": "private-bootstrap-fixture", "APIServerProxy": {
            "Mode": "auth", "ServiceName": "svc:issuer", "IssueCerts": True}}
        config = {"metadata": {"name": r.name + "-issuer-0-config", "namespace": "system",
                               "uid": UID, "resourceVersion": "4", "ownerReferences": [{"kind": "ProxyGroup",
                                   "name": r.name + "-issuer", "uid": OTHER, "controller": True}], "labels": {
                                   "tailscale.com/parent-resource": r.name + "-issuer"}},
                  "data": {"config.hujson": base64.b64encode(json.dumps(original).encode()).decode()}}
        calls = []
        self.assertTrue(installation.bootstrap_issuer(r, proxy, config, apply_patch=lambda *a: calls.append(a)))
        patch = calls[0][2]
        self.assertEqual(patch[0], {"op": "test", "path": "/metadata/uid", "value": UID})
        self.assertEqual(patch[1]["value"], "4")
        updated = json.loads(base64.b64decode(patch[2]["value"]))
        self.assertEqual(updated, original | {"AdvertiseServices": ["svc:issuer"]})
        config["data"]["config.hujson"] = patch[2]["value"]
        self.assertFalse(installation.bootstrap_issuer(r, proxy, config, apply_patch=lambda *a: calls.append(a)))
        self.assertEqual(len(calls), 1)

        updated["AdvertiseServices"] = ["svc:other"]
        config["data"]["config.hujson"] = base64.b64encode(json.dumps(updated).encode()).decode()
        with self.assertRaises(ValueError):
            installation.bootstrap_issuer(r, proxy, config, apply_patch=lambda *a: calls.append(a))
        self.assertEqual(len(calls), 1)

    def test_route_refresh_requires_ready_issuer_and_is_idempotent_without_restarting_pods(self):
        r = registration()
        issuer = {"metadata": {"name": r.name + "-issuer", "uid": OTHER, "generation": 1}, "status": {
            "url": "https://issuer.example.ts.net", "conditions": [{
                "type": "KubeAPIServerProxyConfigured", "status": "False", "lastTransitionTime": "now",
                "observedGeneration": 1}]}}
        service = {"metadata": {"uid": UID, "annotations": {
            "tailscale.com/tailnet-fqdn": "issuer.example.ts.net", "tailscale.com/proxy-group": "egress"}}}
        group = {"metadata": {"name": "egress", "uid": ANCHOR},
                 "spec": {"type": "egress", "tags": [r.issuer_group]}}
        config = {"metadata": {"name": "egress-egress-config", "namespace": "system", "uid": UID,
                               "resourceVersion": "1", "ownerReferences": [{
                                   "kind": "ProxyGroup", "uid": ANCHOR, "controller": True}]},
                  "binaryData": {"egress-services": "preserve-existing-config"}}
        calls = []
        def refresh():
            return installation.refresh_issuer_route(r, issuer, service, group, config,
                                                     apply_patch=lambda *a: calls.append(a))
        with self.assertRaises(ValueError):
            refresh()
        self.assertEqual(calls, [])
        issuer["status"]["conditions"][0]["status"] = "True"
        self.assertTrue(refresh())
        operations = calls[0][2]
        self.assertEqual([o["path"] for o in operations],
                         ["/metadata/uid", "/metadata/resourceVersion", "/data", "/data/" + r.name + "-route"])
        config["data"] = {r.name + "-route": operations[-1]["value"]}
        self.assertFalse(refresh())
        self.assertEqual(config["binaryData"], {"egress-services": "preserve-existing-config"})
        self.assertEqual(len(calls), 1)

    def test_cli_errors_do_not_echo_input_or_write_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"registration": {"secret": "must-never-be-echoed"}}))
            result = subprocess.run([sys.executable, str(DIRECTORY / "credentials.py"), "exec", str(path)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("must-never-be-echoed", result.stderr)


if __name__ == "__main__":
    unittest.main()
