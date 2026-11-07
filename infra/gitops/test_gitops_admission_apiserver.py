# /// script
# dependencies = ["pyyaml==6.0.3"]
# ///
"""Evaluate the GitOps controller admission policy on a real kube-apiserver.

Name checks in the chart tests cannot show whether the CEL actually admits a
rendered workload. This starts envtest's etcd and kube-apiserver
(KUBEBUILDER_ASSETS), installs the rendered policies and server-side dry-runs the
real chart output as the Argo CD application controller.
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ASSETS = os.environ.get("KUBEBUILDER_ASSETS", "")
CONTROLLER = "system:serviceaccount:bravo-gitops:bravo-gitops-application-controller"
HOST = {"image": {"digest": "sha256:" + "a" * 64}, "sourceRevision": "b" * 40,
        "configuration": {"existingConfigMap": "host-config", "sha256": "c" * 64},
        "reader": {"existingConfigMap": "reader-config", "catalogClaim": "existing-catalog"},
        "recordsClaim": "t15-records", "secrets": {"catalogKey": "existing-key", "host": "t15-host", "executor": "t15-executor"},
        "hostAliases": [{"ip": "43.202.75.96", "hostnames": ["sts.ap-northeast-2.amazonaws.com"]}]}


def helm(*args):
    return subprocess.run(["helm", "template", *args, "--kube-version", "1.35.9"],
                          check=True, capture_output=True, text=True, timeout=60).stdout


def values_file(directory, name, value):
    path = Path(directory) / name
    path.write_text(json.dumps(value))
    return str(path)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(ASSETS and Path(ASSETS, "kube-apiserver").is_file(), "KUBEBUILDER_ASSETS not set")
class ControllerAdmission(unittest.TestCase):
    EXTRA, NAMESPACES, CRDS = None, (), ()

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        d = Path(cls.dir.name)
        subprocess.run(["openssl", "genrsa", "-out", str(d / "sa.key"), "2048"], check=True, capture_output=True)
        token = secrets.token_hex(16)
        (d / "tokens.csv").write_text(f"{token},admin,admin,system:masters\n")
        etcd_port, api_port = free_port(), free_port()
        cls.procs = [subprocess.Popen(
            [str(Path(ASSETS, "etcd")), "--data-dir", str(d / "etcd"),
             "--listen-client-urls", f"http://127.0.0.1:{etcd_port}",
             "--advertise-client-urls", f"http://127.0.0.1:{etcd_port}",
             "--listen-peer-urls", f"http://127.0.0.1:{free_port()}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
        cls.procs.append(subprocess.Popen(
            [str(Path(ASSETS, "kube-apiserver")), "--etcd-servers", f"http://127.0.0.1:{etcd_port}",
             "--bind-address", "127.0.0.1", "--secure-port", str(api_port), "--cert-dir", str(d / "certs"),
             "--token-auth-file", str(d / "tokens.csv"), "--authorization-mode", "AlwaysAllow",
             "--service-account-issuer", "https://kubernetes.default.svc.cluster.local",
             "--service-account-key-file", str(d / "sa.key"),
             "--service-account-signing-key-file", str(d / "sa.key"),
             "--service-cluster-ip-range", "10.0.0.0/24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        cls.kubectl = [str(Path(ASSETS, "kubectl")), "--server", f"https://127.0.0.1:{api_port}",
                       "--insecure-skip-tls-verify", "--token", token, "--request-timeout", "20s"]
        for _ in range(120):
            if cls.kube(["get", "--raw", "/readyz"]).returncode == 0:
                break
            time.sleep(0.5)
        else:
            cls.tearDownClass()
            raise RuntimeError("kube-apiserver did not become ready")
        for ns in ("platform-state", "bravo-gitops", *cls.NAMESPACES):
            cls.kube(["create", "namespace", ns], check=True)
        for crd in cls.CRDS:
            cls.kube(["apply", "-f", "-"], check=True, data=crd)
        runtime = {k: HOST[k] for k in ("configuration", "reader", "recordsClaim", "secrets", "hostAliases")}
        reader = {"configuration": {"existingConfigMap": "reader-config", "sha256": "c" * 64},
                  "secrets": {"catalogKey": "existing-key", "tls": "existing-tls"}, "catalogClaim": "existing-catalog"}
        argo = helm("bravo-gitops", str(ROOT / "infra/gitops/argocd"), "--namespace", "bravo-gitops",
                    "-f", values_file(d, "argo.json", {"rehearsalHost": {"enabled": True, "runtime": runtime},
                                                       "productReader": {"enabled": True, "runtime": reader}}),
                    *(["-f", str(cls.EXTRA)] if cls.EXTRA else []))
        policies = [o for o in yaml.safe_load_all(argo) if o and o["kind"].startswith("ValidatingAdmissionPolicy")]
        cls.kube(["apply", "-f", "-"], check=True, data={"apiVersion": "v1", "kind": "List", "items": policies})
        # A binding is enforced asynchronously; wait until a known violation is denied.
        probe = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "not-adopted"}, "spec": {"ports": [{"port": 1}]}}
        for _ in range(120):
            if cls.create(probe).returncode != 0:
                break
            time.sleep(0.5)
        else:
            cls.tearDownClass()
            raise RuntimeError("admission policy never became active")

    @classmethod
    def tearDownClass(cls):
        for p in reversed(cls.procs):
            p.terminate()
            p.wait(timeout=30)
        cls.dir.cleanup()

    @classmethod
    def kube(cls, args, *, check=False, data=None):
        return subprocess.run(cls.kubectl + args, input=None if data is None else json.dumps(data),
                              check=check, capture_output=True, text=True, timeout=60)

    @classmethod
    def create(cls, obj):
        namespace = obj.get("metadata", {}).get("namespace", "platform-state")
        return cls.kube(["--as", CONTROLLER, "create", "--dry-run=server", "-n", namespace, "-f", "-"], data=obj)

    def rendered_host(self):
        with tempfile.TemporaryDirectory() as d:
            out = helm("rehearsal-host", str(ROOT / "infra/charts/rehearsal-host"), "--namespace", "platform-state",
                       "-f", values_file(d, "host.json", HOST))
        return {o["kind"]: o for o in yaml.safe_load_all(out) if o}

    def test_every_gitops_workload_chart_is_admitted(self):
        # Each Argo Application's real chart output must pass the controller policy it runs under.
        sys.path.insert(0, str(Path(__file__).parent))
        import test_product_reader_chart
        import test_registration_chart
        for name, module in (("registration", test_registration_chart), ("product-reader", test_product_reader_chart)):
            result = module.render(module.VALUES)
            self.assertEqual(result.returncode, 0, result.stderr)
            for obj in (o for o in yaml.safe_load_all(result.stdout) if o):
                with self.subTest(chart=name, kind=obj["kind"]):
                    created = self.create(obj)
                    self.assertEqual(created.returncode, 0, created.stderr)

    def test_rendered_host_workload_is_admitted(self):
        for kind, obj in self.rendered_host().items():
            with self.subTest(kind=kind):
                result = self.create(obj)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_host_exception_admits_only_its_own_identity_and_intake_token(self):
        def mutate(change):
            dep = deepcopy(self.rendered_host()["Deployment"])
            change(dep["spec"]["template"]["spec"])
            return dep
        def token(pod):
            return next(v for v in pod["volumes"] if v["name"] == "intake-token")["projected"]["sources"]
        faults = {
            "other service account": lambda p: p.update(serviceAccountName="platform-runtime"),
            "other audience": lambda p: token(p)[0]["serviceAccountToken"].update(audience="kubernetes"),
            "longer token": lambda p: token(p)[0]["serviceAccountToken"].update(expirationSeconds=3600),
            "extra projected source": lambda p: token(p).append({"configMap": {"name": "kube-root-ca.crt"}}),
            "second projected volume": lambda p: p["volumes"].append({"name": "other", "projected": {"sources": [
                {"serviceAccountToken": {"audience": "bravo-t10-rehearsal-intake", "expirationSeconds": 600, "path": "t"}}]}}),
            "host path": lambda p: p["volumes"].append({"name": "root", "hostPath": {"path": "/"}}),
        }
        for name, change in faults.items():
            with self.subTest(fault=name):
                self.assertNotEqual(self.create(mutate(change)).returncode, 0, name)

    def test_other_workloads_still_cannot_use_projected_tokens(self):
        dep = deepcopy(self.rendered_host()["Deployment"])
        dep["metadata"]["name"] = "t10-registration"
        dep["spec"]["template"]["spec"]["serviceAccountName"] = "t10-registration"
        self.assertNotEqual(self.create(dep).returncode, 0)



def kustomized(path):
    out = subprocess.run(["kubectl", "kustomize", str(ROOT / path)], check=True, capture_output=True, text=True, timeout=60)
    return [o for o in yaml.safe_load_all(out.stdout) if o]


EXTERNAL_SECRET_CRD = {
    "apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
    "metadata": {"name": "externalsecrets.external-secrets.io"},
    "spec": {"group": "external-secrets.io", "scope": "Namespaced",
             "names": {"plural": "externalsecrets", "singular": "externalsecret", "kind": "ExternalSecret"},
             "versions": [{"name": "v1", "served": True, "storage": True,
                           "schema": {"openAPIV3Schema": {"type": "object", "x-kubernetes-preserve-unknown-fields": True}}}]}}


class EntryAndObservabilityAdmission(ControllerAdmission):
    """The development values (public entry, observability) under the same controller policies."""
    EXTRA = ROOT / "infra/gitops/environments/development/public-entry.json"
    NAMESPACES = ("bravo-observability", "platform-telemetry")
    CRDS = (EXTERNAL_SECRET_CRD,)

    def bundles(self):
        return kustomized("infra/envs/public-entry") + kustomized("services/observability/deploy/platform-gitops")

    def test_rendered_bundles_are_admitted(self):
        for obj in self.bundles():
            with self.subTest(kind=obj["kind"], name=obj["metadata"]["name"]):
                result = self.create(obj)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_names_identities_and_grants_stay_bounded(self):
        objs = {(o["kind"], o["metadata"]["name"]): o for o in self.bundles()}
        def changed(key, change):
            obj = deepcopy(objs[key])
            change(obj)
            return obj
        faults = {
            "other ConfigMap in platform-state": changed(("ConfigMap", "public-entry-spec"),
                                                        lambda o: o["metadata"].update(name="other")),
            "Collector in platform-state": changed(("Deployment", "bravo-collector"),
                                                   lambda o: o["metadata"].update(namespace="platform-state")),
            "public entry with an API token": changed(("Deployment", "public-entry"),
                                                      lambda o: o["spec"]["template"]["spec"].update(automountServiceAccountToken=True)),
            "other credential store": changed(("ExternalSecret", "public-entry-cloudflare"),
                                              lambda o: o["spec"]["secretStoreRef"].update(name="t17-edge-secret-reader")),
        }
        for name, obj in faults.items():
            with self.subTest(fault=name):
                self.assertNotEqual(self.create(obj).returncode, 0, name)


if __name__ == "__main__":
    if not ASSETS:
        sys.exit("set KUBEBUILDER_ASSETS to envtest binaries")
    unittest.main()
