"""Serve the Kubernetes review and read APIs over verified loopback TLS."""

import json
import ssl
import subprocess
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from bravo_platform_events.registration_probe import (
    EnvironmentRef,
    KubernetesProbe,
    ProbeUnavailable,
    SourceRelease,
)

DIGEST = "sha256:" + "a" * 64


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    folder = tmp_path_factory.mktemp("registration-tls")
    cert, key = folder / "cert", folder / "key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                    "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, capture_output=True)
    faults = set()
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def response(self, value, status=200):
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def do_POST(self):
            calls.append(self.path)
            if self.headers.get("Authorization") != "Bearer fixture":
                return self.response({}, 403)
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("selfsubjectreviews"):
                subject = "admin" if "actor" in faults else "system:serviceaccount:ns:bravo-collector"
                return self.response({"status": {"userInfo": {"username": subject}}})
            if self.path.endswith("selfsubjectrulesreviews"):
                rule = {"verbs": ["get"], "resources": ["pods"]}
                if "write" in faults:
                    rule = {"verbs": ["patch"], "resources": ["configmaps"], "resourceNames": ["one"]}
                return self.response({"status": {"incomplete": "incomplete" in faults, "resourceRules": [rule]}})
            attrs = body["spec"]["resourceAttributes"]
            assert attrs["namespace"] == "ns"
            allowed = "write" in faults if attrs["verb"] in ("patch", "create") else "denied" not in faults
            return self.response({"status": {"allowed": allowed,
                                             "evaluationError": "fault" if "unknown" in faults else ""}})

        def do_GET(self):
            calls.append(self.path)
            if "redirect" in faults:
                self.send_response(307)
                self.send_header("Location", self.path + "/unexpected")
                self.end_headers()
            elif self.path == "/api/v1/namespaces/kube-system":
                if "cluster-denied" in faults:
                    return self.response({}, 403)
                self.response({"metadata": {"name": "kube-system", "uid": "kube-system-uid"}})
            elif "/clusters/" in self.path:
                self.response({"kind": "Cluster", "metadata": {"namespace": "ns", "name": "db", "uid": "physical-db"}})
            else:
                assert self.path.startswith("/api/v1/namespaces/ns/pods?")
                pod = {"metadata": {"namespace": "ns", "labels": {"app": "demo"}, "ownerReferences": [dict(controller=True, apiVersion="apps/v1", kind="ReplicaSet", uid="other" if "owner" in faults else "rs-uid")]},
                       "spec": {"serviceAccountName": "app-sa", "containers": [{"name": "worker",
                                "image": "demo@" + DIGEST, "env": [{"name": "KEY", "value": "PRIVATE_SENTINEL"}],
                                "envFrom": [{"secretRef": {"name": "db-config"}}]}]},
                       "status": {"containerStatuses": [{"name": "worker", "ready": "unready" not in faults,
                                                         "imageID": "demo@" + ("sha256:" + "c"*64 if "image" in faults else DIGEST)}]}}
                self.response({"metadata": {"continue": "cursor" if "partial" in faults else ""},
                               "items": [] if "empty" in faults else [pod]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield cert, server.server_port, faults, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.fixture
def reader(tls):
    cert, port, faults, calls = tls
    faults.clear()
    calls.clear()
    env = EnvironmentRef("t", "source", "aws", "ns", "db", "physical-db",
                         f"https://127.0.0.1:{port}", cert, lambda: "fixture", "localhost")
    return env, faults, calls


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    repo = tmp_path_factory.mktemp("registered-repo")
    def git(*args):
        return subprocess.check_output(["git", "-C", repo, *args], text=True, stderr=subprocess.DEVNULL).strip()
    git("init", "-q")
    git("remote", "add", "origin", "https://example.invalid/demo")
    (repo / "app").mkdir()
    (repo / "app/main").write_text("fixture")
    (repo / "compose.yaml").write_text("fixture")
    git("add", "app", "compose.yaml")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    return SourceRelease("t", "source", "https://example.invalid/demo", repo, "app", "compose.yaml",
                         git("rev-parse", "HEAD"), "demo", "worker", "demo@" + DIGEST, (DIGEST,), "app-sa", "rs-uid")


def test_tls_connection_permissions_and_real_git_objects(reader, release):
    env, faults, calls = reader
    faults.update(("write", "incomplete"))
    obs = KubernetesProbe().inspect(env, release)
    assert obs["write_checks"] == {"create_pods": "allowed", "patch_registered_cnpg": "allowed"}
    assert set(obs["warnings"]) == {"write_scope_unknown", "namespace_write_granted"}
    assert obs["resources"] == [{"kind": "Cluster", "name": "db"}, {"kind": "Secret", "name": "db-config"},
                                {"kind": "ServiceAccount", "name": "app-sa"}]
    assert obs["repo_deploy_match"] and obs["source_sha"] == release.revision
    assert "PRIVATE_SENTINEL" not in json.dumps(obs)
    assert not any("/secrets" in path for path in calls)


@pytest.mark.parametrize(("fault", "code"), [
    ("actor", "collector_identity_mismatch"), ("denied", "required_read_denied"),
    ("unknown", "required_read_denied"), ("redirect", "registration_probe_unavailable"),
    ("partial", "incomplete_pod_read"), ("empty", "workload_unavailable"),
    ("owner", "deployment_release_mismatch"), ("image", "deployment_release_mismatch"), ("unready", "deployment_release_mismatch")])
def test_bad_observation_blocks_registration(reader, release, fault, code):
    env, faults, calls = reader
    faults.add(fault)
    with pytest.raises(ProbeUnavailable, match=code):
        KubernetesProbe().inspect(env, release)
    assert not any("unexpected" in path for path in calls)


def test_wrong_server_name_blocks_request_before_bearer_reaches_api(reader):
    env, _, calls = reader
    with pytest.raises(ProbeUnavailable):
        KubernetesProbe().inspect(replace(env, tls_name="wrong.invalid"))
    assert calls == []


@pytest.mark.parametrize("change", [{"repo_url": "https://wrong.invalid/app"}, {"revision": "d"*40},
                                     {"iac_path": "missing.yaml"}, {"app_path": "compose.yaml"}])
def test_checkout_must_contain_the_approved_source(release, change):
    with pytest.raises(ProbeUnavailable):
        replace(release, **change).verify_checkout()


def test_cluster_identity_is_verified_only_when_configured(reader):
    env, faults, calls = reader
    assert "cluster_identity" not in KubernetesProbe().inspect(env)
    assert "/api/v1/namespaces/kube-system" not in calls
    obs = KubernetesProbe().inspect(replace(env, cluster_uid="kube-system-uid"))
    assert len(obs["cluster_identity"]) == 64 and "kube-system-uid" not in json.dumps(obs)
    with pytest.raises(ProbeUnavailable, match="environment_replaced"):
        KubernetesProbe().inspect(replace(env, cluster_uid="another-cluster"))
    faults.add("cluster-denied")
    with pytest.raises(ProbeUnavailable, match="collector_denied"):
        KubernetesProbe().inspect(replace(env, cluster_uid="kube-system-uid"))
