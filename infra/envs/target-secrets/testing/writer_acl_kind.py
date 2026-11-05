#!/usr/bin/env python3
"""Local-only check of the T15 rehearsal writer (ADR-0029 A안) against a real OpenBao and Kubernetes.

Runs on a throwaway kind cluster (context ``kind-*`` only): an upstream OpenBao dev server
stands in for ``target-openbao``, the *rendered* NetworkPolicy, operator reader policy, writer
policy and roles are applied unchanged, and real ServiceAccount tokens log in through Kubernetes
TokenReview. Prints and writes only case names and outcomes; tokens and synthetic values never
leave memory.

This is not the demo RKE2 target: dev mode is HTTP, in-memory and unsealed, and the image is
upstream ``quay.io/openbao/openbao:2.7.2`` (same source commit as the team's patched build).
"""
import argparse
import importlib.util
import json
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("target_secrets_render", HERE.parent / "render.py")
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)

IMAGE = "quay.io/openbao/openbao@sha256:5046911075e137e154ce49ab051502f4ec7cf367d484ae4a4dce665d9b90561f"
NS_A, NS_B = "rehearsal-aaa", "rehearsal-bbb"
NS_PREFIX = "rehearsal-aaab"  # KV-path prefix confusion only: "rehearsal-aaa/*" must not match it
READER = render.READER_ACCOUNT
RESTRICTED = {"allowPrivilegeEscalation": False, "runAsNonRoot": True, "capabilities": {"drop": ["ALL"]},
              "seccompProfile": {"type": "RuntimeDefault"}}
POD_SECURITY = {"runAsNonRoot": True, "runAsUser": 100, "runAsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"}}
BAO_HTTP = "http://target-openbao.bravo-secrets.svc:8200"
# Label of the test-only reader ingress peer (the rendered ``network`` policy has none; README).
READER_POD_LABEL = {"bravo-test-rehearsal-reader": "true"}


class Kube:
    def __init__(self, kubeconfig):
        self.base = ["kubectl", "--kubeconfig", kubeconfig]

    def run(self, *args, stdin=None, check=True):
        result = subprocess.run(self.base + list(args), input=stdin, capture_output=True, text=True, check=False)
        if check and result.returncode != 0:
            raise SystemExit(f"kubectl {args[0]} failed: {result.stderr.strip()[:300]}")
        return result

    def apply(self, doc, dry_run=False):
        if not dry_run:
            return self.run("apply", "-f", "-", stdin=json.dumps(doc)).stdout.strip()
        result = self.run("apply", "--dry-run=server", "-f", "-", stdin=json.dumps(doc))
        return (result.stdout + result.stderr).strip()

    def token(self, namespace, account, audience="openbao"):
        args = ["create", "token", account, "-n", namespace, "--duration", "10m"]
        if audience:
            args += ["--audience", audience]
        return self.run(*args).stdout.strip()


def guard(kube):
    context = kube.run("config", "current-context").stdout.strip()
    nodes = json.loads(kube.run("get", "nodes", "-o", "json").stdout)["items"]
    if not context.startswith("kind-") or len(nodes) != 1 or not nodes[0]["metadata"]["name"].endswith("-control-plane"):
        raise SystemExit("refusing: this check runs only on a single-node local kind cluster")


def lst(*items):
    return {"apiVersion": "v1", "kind": "List", "items": list(items)}


def ns(name, **labels):
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": {
        "pod-security.kubernetes.io/enforce": "restricted", **labels}}}


def bao_standin(root_token):
    labels = {"app.kubernetes.io/name": "openbao", "app.kubernetes.io/instance": render.RELEASE, "component": "server"}
    meta = {"namespace": render.NAMESPACE}
    secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "dev-root", **meta},
              "stringData": {"token": root_token}}
    account = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": render.RELEASE, **meta}}
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": f"{render.RELEASE}-0", "labels": labels, **meta},
           "spec": {"serviceAccountName": render.RELEASE, "securityContext": POD_SECURITY, "containers": [{
               "name": "openbao", "image": IMAGE, "command": ["bao", "server", "-dev", "-dev-no-store-token"],
               "env": [{"name": "BAO_DEV_LISTEN_ADDRESS", "value": "0.0.0.0:8200"},
                       {"name": "BAO_DEV_ROOT_TOKEN_ID", "valueFrom": {"secretKeyRef": {"name": "dev-root", "key": "token"}}},
                       {"name": "HOME", "value": "/tmp"}],
               "ports": [{"containerPort": 8200}], "securityContext": RESTRICTED,
               "readinessProbe": {"exec": {"command": ["bao", "status", "-address=http://127.0.0.1:8200"]},
                                  "periodSeconds": 2}}]}}
    service = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": render.RELEASE, **meta},
               "spec": {"selector": labels, "ports": [{"port": 8200, "targetPort": 8200}]}}
    return lst(secret, account, pod, service)


def probe_pod(name, namespace, labels):
    """Connect to OpenBao through the Service; exit 0 only if the HTTP status call succeeds."""
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False, "securityContext": POD_SECURITY,
                     "containers": [{"name": "probe", "image": IMAGE, "command": ["bao", "status", "-format=json"],
                                     "env": [{"name": "BAO_ADDR", "value": BAO_HTTP},
                                             {"name": "BAO_CLIENT_TIMEOUT", "value": "8s"}, {"name": "HOME", "value": "/tmp"}],
                                     "securityContext": RESTRICTED}]}}


def test_only_reader_peer():
    """Kind-only, additive NetworkPolicy so the reader Pod in ``rehearsal-aaa`` can reach the
    stand-in. Not rendered by ``render.py``: the reader peer is still an open contract item."""
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "kind-test-only-reader-peer", "namespace": render.NAMESPACE},
            "spec": {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "openbao"}}, "policyTypes": ["Ingress"],
                     "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NS_A}},
                                            "podSelector": {"matchLabels": READER_POD_LABEL}}],
                                  "ports": [{"protocol": "TCP", "port": 8200}]}]}}


WRITER_PREPARE = r"""
set -u
export BAO_ADDR=http://target-openbao.bravo-secrets.svc:8200 BAO_CLIENT_TIMEOUT=10s HOME=/tmp
r() { if "$@" >/dev/null 2>&1; then echo allow; else echo deny; fi; }
JWT=$(cat /var/run/secrets/openbao/token)
TOKEN=$(bao write -field=token auth/kubernetes/login role=rehearsal-openbao-writer jwt="$JWT" 2>/dev/null) || TOKEN=''
[ -n "$TOKEN" ] && echo "RESULT pod_login allow" || { echo "RESULT pod_login deny"; exit 1; }
export BAO_TOKEN="$TOKEN"
echo "RESULT pod_kv_put_namespace_path $(r bao kv put -mount=secret -cas=0 "$(cat /work/remote)" credentials_json=@/work/synthetic.json)"
echo "RESULT pod_kv_get_namespace_path $(r bao kv get -mount=secret "$(cat /work/remote)")"
echo "RESULT pod_kv_get_target $(r bao kv get -mount=secret bravo/target/precutover/database)"
echo "RESULT pod_policy_write_operator_reader $(r bao policy write rehearsal-app-reader /work/evil.hcl)"
echo "RESULT pod_policy_write_evil $(r bao policy write rehearsal-app-reader-evil /work/evil.hcl)"
echo "RESULT pod_reader_role_write $(r sh -c 'bao write "auth/kubernetes/role/$(cat /work/role-name)" - < /work/role.json')"
echo "RESULT pod_revoke_self $(r bao token revoke -self)"
"""

WRITER_CLEANUP = r"""
set -u
export BAO_ADDR=http://target-openbao.bravo-secrets.svc:8200 BAO_CLIENT_TIMEOUT=10s HOME=/tmp
r() { if "$@" >/dev/null 2>&1; then echo allow; else echo deny; fi; }
JWT=$(cat /var/run/secrets/openbao/token)
TOKEN=$(bao write -field=token auth/kubernetes/login role=rehearsal-openbao-writer jwt="$JWT" 2>/dev/null) || TOKEN=''
[ -n "$TOKEN" ] && echo "RESULT pod_cleanup_login allow" || { echo "RESULT pod_cleanup_login deny"; exit 1; }
export BAO_TOKEN="$TOKEN"
echo "RESULT pod_reader_role_delete $(r bao delete "auth/kubernetes/role/$(cat /work/role-name)")"
echo "RESULT pod_kv_metadata_delete $(r bao kv metadata delete -mount=secret "$(cat /work/remote)")"
echo "RESULT pod_cleanup_revoke_self $(r bao token revoke -self)"
"""


def writer_job(name, script, namespace):
    work = {"synthetic.json": json.dumps({"username": "rehearsal", "password": "synthetic-" + secrets.token_hex(8)}),
            "role.json": json.dumps(render.rehearsal_auth_role(namespace)),
            "role-name": render.rehearsal_role_name(namespace),
            "remote": render.rehearsal_remote_path(namespace),
            "evil.hcl": 'path "secret/data/bravo/target/*" { capabilities = ["read"] }\n',
            "job.sh": script}
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": render.WRITER_NAMESPACE},
              "data": work}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": render.WRITER_NAMESPACE},
           "spec": {"backoffLimit": 0, "template": {"metadata": {"labels": dict(render.WRITER_POD_LABEL)}, "spec": {
               "restartPolicy": "Never", "serviceAccountName": render.WRITER_ACCOUNT, "automountServiceAccountToken": False,
               "securityContext": POD_SECURITY,
               "containers": [{"name": "writer", "image": IMAGE, "command": ["sh", "/work/job.sh"], "securityContext": RESTRICTED,
                               "volumeMounts": [{"name": "work", "mountPath": "/work", "readOnly": True},
                                                {"name": "token", "mountPath": "/var/run/secrets/openbao", "readOnly": True}]}],
               "volumes": [{"name": "work", "configMap": {"name": name}},
                           {"name": "token", "projected": {"sources": [{"serviceAccountToken": {
                               "audience": "openbao", "expirationSeconds": 600, "path": "token"}}]}}]}}}}
    return lst(config, job)


# Only these substitutions adapt the rendered init container to the HTTP dev stand-in; the
# login role, KV path, temp-file/atomic-replace and revoke logic run exactly as rendered.
INIT_SUBSTITUTIONS = [("https://target-openbao.bravo-secrets.svc:8200", BAO_HTTP), (" BAO_CACERT=/openbao/ca/ca.crt", "")]


def reader_pod(name, namespace, labels):
    init = render.rehearsal_fetch_init_container(namespace)
    script = init["command"][2]
    for old, new in INIT_SUBSTITUTIONS:
        if old not in script:
            raise SystemExit(f"rendered init script no longer contains {old!r}")
        script = script.replace(old, new)
    init = dict(init, image=IMAGE, command=["/bin/sh", "-c", script],
                env=[{"name": "BAO_CLIENT_TIMEOUT", "value": "10s"}],
                securityContext=dict(init["securityContext"], seccompProfile={"type": "RuntimeDefault"}),
                volumeMounts=[m for m in init["volumeMounts"] if m["name"] != "openbao-ca"])
    init.pop("x-bravo-remote-path")
    check = ("f=/run/bravo/database/credentials.json; "
             "if [ -s \"$f\" ] && grep -q '\"password\"' \"$f\"; then echo \"RESULT credentials_file ok mode=$(stat -c %a \"$f\")\"; "
             "else echo 'RESULT credentials_file missing'; exit 1; fi; "
             "echo \"RESULT tmp_files $(ls -A /run/bravo/database | grep -c '^.credentials.json.tmp' || true)\"")
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {"restartPolicy": "Never", "serviceAccountName": READER, "automountServiceAccountToken": False,
                     "securityContext": POD_SECURITY, "initContainers": [init],
                     "containers": [{"name": "app", "image": IMAGE, "command": ["/bin/sh", "-c", check],
                                     "securityContext": RESTRICTED,
                                     "volumeMounts": [{"name": "bravo-database", "mountPath": "/run/bravo/database",
                                                       "readOnly": True}]}],
                     "volumes": [{"name": "bravo-database", "emptyDir": {"medium": "Memory"}},
                                 {"name": "openbao-token", "projected": {"sources": [{"serviceAccountToken": {
                                     "audience": "openbao", "expirationSeconds": 600, "path": "token"}}]}}]}}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # record a redirect as its own status instead of following it


class Bao:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}/v1/"
        self.opener = urllib.request.build_opener(NoRedirect)
        self.location = None

    def call(self, method, path, token=None, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            request.add_header("X-Vault-Token", token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            self.location = error.headers.get("Location")
            raw = error.read()
            try:
                return error.code, json.loads(raw) if raw else {}
            except ValueError:
                return error.code, {}

    def login(self, role, jwt):
        status, body = self.call("POST", "auth/kubernetes/login", body={"role": role, "jwt": jwt})
        return body.get("auth", {}).get("client_token") if status == 200 else None


def outcome(status):
    """``deny`` only for the ACL's 403; any other error means the ACL let the request through."""
    if status == 403:
        return "deny"
    if 300 <= status < 400:
        return "redirect"
    return "allow" if 200 <= status < 300 else f"allow-but-backend-{status}"


def wait_ready(kube, namespace, name, seconds=180):
    kube.run("wait", "--for=condition=Ready", f"pod/{name}", "-n", namespace, f"--timeout={seconds}s")


def pod_exit(kube, namespace, name, seconds=90):
    deadline = time.time() + seconds
    while time.time() < deadline:
        state = json.loads(kube.run("get", "pod", name, "-n", namespace, "-o", "json").stdout)
        statuses = state.get("status", {}).get("containerStatuses") or []
        terminated = statuses and statuses[0].get("state", {}).get("terminated")
        if terminated:
            return terminated["exitCode"]
        time.sleep(2)
    raise SystemExit(f"pod {name} did not finish")


def pod_phase(kube, namespace, name, seconds=120):
    """Final phase of a ``restartPolicy: Never`` Pod plus the init container's exit code."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        state = json.loads(kube.run("get", "pod", name, "-n", namespace, "-o", "json").stdout).get("status", {})
        if state.get("phase") in ("Succeeded", "Failed"):
            init = (state.get("initContainerStatuses") or [{}])[0].get("state", {}).get("terminated", {})
            return state["phase"], init.get("exitCode")
        time.sleep(2)
    raise SystemExit(f"pod {name} did not finish")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--output", required=True, help="private results file outside the repository")
    args = parser.parse_args()
    kube = Kube(args.kubeconfig)
    guard(kube)
    results = {"design": "ADR-0029 A안", "cases": [], "dry_run": {}, "network": {}}

    def case(name, expected, actual, note=""):
        results["cases"].append({"name": name, "expected": expected, "actual": actual,
                                 "pass": actual == expected, **({"note": note} if note else {})})
        print(f"{'PASS' if actual == expected else 'FAIL'} {name}: expected={expected} actual={actual}")

    # 1. Rendered manifests: server-side dry-run, then apply the parts the stand-in needs.
    api_ip = kube.run("get", "svc", "kubernetes", "-n", "default", "-o", "jsonpath={.spec.clusterIP}").stdout
    endpoint_ip = kube.run("get", "endpointslices", "-n", "default", "-l", "kubernetes.io/service-name=kubernetes",
                           "-o", "jsonpath={.items[0].endpoints[0].addresses[0]}").stdout
    foundation = render.foundation(json.loads(kube.run("get", "nodes", "-o", "json").stdout)["items"][0]["metadata"]["name"])
    identity = render.rehearsal_writer_identity()
    for name, namespace in [("foundation-namespace", foundation["items"][0]), ("eso-namespace", render.eso_namespace()),
                            ("rehearsal-writer-namespace", identity["items"][0])]:
        results["dry_run"][name] = kube.apply(namespace, dry_run=True).splitlines()  # fresh creation
        kube.apply(namespace)  # namespaced objects below need it to exist even for a dry run
    for name, doc in [("foundation", foundation), ("network", render.network(api_ip, endpoint_ip)),
                      ("eso-network", render.eso_network(api_ip, endpoint_ip)),
                      ("rehearsal-writer-identity", identity)]:
        results["dry_run"][name] = kube.apply(doc, dry_run=True).splitlines()
    bao_rendered = Path(args.output).with_name("bao.json")
    if bao_rendered.exists():  # finalize-bao output of the pinned chart, when the operator rendered it
        results["dry_run"]["finalize-bao"] = kube.apply(json.loads(bao_rendered.read_text()), dry_run=True).splitlines()
    kube.apply(lst(*[i for i in foundation["items"] if i["kind"] in ("ClusterRole", "ClusterRoleBinding")]))
    kube.apply(render.rehearsal_writer_identity())
    kube.apply(lst(ns(NS_A, **render.REHEARSAL_LABEL), ns(NS_B, **render.REHEARSAL_LABEL), ns("target-dev"),
                   ns(render.LEGACY_WRITER_NAMESPACE)))
    for namespace in (NS_A, NS_B, render.WRITER_NAMESPACE, "target-dev"):  # the reader SA name in every namespace
        kube.apply({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": READER, "namespace": namespace}})
    results["dry_run"]["rehearsal-delivery-a"] = kube.apply(
        lst(*[i for i in render.rehearsal_delivery(NS_A)["items"] if i["kind"] == "ServiceAccount"]), dry_run=True).splitlines()
    root = secrets.token_urlsafe(24)
    kube.apply(bao_standin(root))
    kube.apply(render.network(api_ip, endpoint_ip))
    wait_ready(kube, render.NAMESPACE, f"{render.RELEASE}-0")

    port = free_port()
    forward = subprocess.Popen(kube.base + ["port-forward", "-n", render.NAMESPACE, f"svc/{render.RELEASE}", f"{port}:8200"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        bao = Bao(port)
        for _ in range(30):
            try:
                if bao.call("GET", "sys/health")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        results["openbao_version"] = bao.call("GET", "sys/health")[1].get("version")

        # 2. Operator install (README step 8 + rehearsal subtree): enable auth, read the mount
        #    accessor, write the templated reader policy once, then the writer policy and role.
        for method, path, body in [
            ("POST", "sys/auth/kubernetes", {"type": "kubernetes"}),
            ("POST", "auth/kubernetes/config", {"kubernetes_host": "https://kubernetes.default.svc"}),
            ("POST", "secret/config", {"cas_required": True}),
        ]:
            status, body_out = bao.call(method, path, root, body)
            if status >= 300:
                raise SystemExit(f"setup {path} failed: {status} {body_out.get('errors')}")
        mount = bao.call("GET", "sys/auth/kubernetes", root)[1]
        accessor = mount.get("accessor") or mount.get("data", {}).get("accessor")
        listed = bao.call("GET", "sys/auth", root)[1]
        listed_accessor = (listed.get("data") or listed).get("kubernetes/", {}).get("accessor")
        case("operator_accessor_read_matches_auth_list", True, bool(accessor) and accessor == listed_accessor)
        case("operator_accessor_format", True, bool(render.KUBERNETES_ACCESSOR_RE.fullmatch(accessor or "")))
        # The README's operator command, run with the real CLI inside the server Pod.
        cli = kube.run("exec", "-i", "-n", render.NAMESPACE, f"{render.RELEASE}-0", "--", "sh", "-s", stdin=(
            'export BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$BAO_DEV_ROOT_TOKEN_ID"; cd /tmp; '
            "bao read -field=accessor sys/auth/kubernetes\n"), check=False).stdout.strip()
        case("operator_cli_read_field_accessor", True, cli == accessor)
        reader_policy = render.rehearsal_reader_policy(accessor)
        for method, path, body in [
            ("PUT", f"sys/policies/acl/{render.READER_POLICY}", {"policy": reader_policy}),
            ("PUT", "sys/policies/acl/rehearsal-openbao-writer", {"policy": render.rehearsal_writer_policy()}),
            ("POST", "auth/kubernetes/role/rehearsal-openbao-writer", render.rehearsal_writer_auth_role()),
            ("POST", "secret/data/bravo/target/precutover/database",
             {"options": {"cas": 0}, "data": {"credentials_json": "synthetic-" + secrets.token_hex(8)}}),
            # Fail-closed check only: the same template rendered with a wrong accessor, and a
            # root-made role that attaches it. Removed before the Pod phase.
            ("PUT", "sys/policies/acl/t9-check-wrong-accessor", {"policy": render.rehearsal_reader_policy("auth_kubernetes_00000000")}),
            ("POST", "auth/kubernetes/role/t9-check-wrong-accessor",
             dict(render.rehearsal_auth_role(NS_A), token_policies="t9-check-wrong-accessor")),
        ]:
            status, body_out = bao.call(method, path, root, body)
            if status >= 300:
                raise SystemExit(f"setup {path} failed: {status} {body_out.get('errors')}")
        stored = bao.call("GET", f"sys/policies/acl/{render.READER_POLICY}", root)[1].get("data", {}).get("policy")
        case("operator_reader_policy_stored_as_rendered", True, stored == reader_policy)

        # 3. Writer identity: only the exact SA, namespace and audience log in.
        writer = bao.login("rehearsal-openbao-writer", kube.token(render.WRITER_NAMESPACE, render.WRITER_ACCOUNT))
        case("writer_login_exact_sa_audience", "allow", "allow" if writer else "deny")
        case("writer_login_default_audience", "deny",
             "allow" if bao.login("rehearsal-openbao-writer", kube.token(render.WRITER_NAMESPACE, render.WRITER_ACCOUNT, None)) else "deny")
        case("writer_role_other_sa", "deny",
             "allow" if bao.login("rehearsal-openbao-writer", kube.token(NS_A, READER)) else "deny")
        case("writer_role_reader_sa_in_control", "deny",
             "allow" if bao.login("rehearsal-openbao-writer", kube.token(render.WRITER_NAMESPACE, READER)) else "deny")
        if not writer:
            raise SystemExit("writer login failed; cannot continue")
        lookup = bao.call("GET", "auth/token/lookup-self", writer)[1].get("data", {})
        results["writer_token"] = {"policies": lookup.get("policies"), "ttl": lookup.get("ttl")}
        case("writer_token_policies_only_writer", ["rehearsal-openbao-writer"], lookup.get("policies"))

        synthetic = {"credentials_json": json.dumps({"username": "rehearsal", "password": "synthetic-" + secrets.token_hex(8)})}
        path_a, path_b, path_prefix = (f"secret/data/{render.rehearsal_remote_path(n)}" for n in (NS_A, NS_B, NS_PREFIX))
        role_a, role_b = (f"auth/kubernetes/role/{render.rehearsal_role_name(n)}" for n in (NS_A, NS_B))
        role_cross = "auth/kubernetes/role/rehearsal-app-reader-crossname"
        allowed = [
            ("kv_put_namespace_a", "POST", path_a, {"options": {"cas": 0}, "data": synthetic}),
            ("kv_put_namespace_b", "POST", path_b, {"options": {"cas": 0}, "data": synthetic}),
            ("kv_put_namespace_prefix_sibling", "POST", path_prefix, {"options": {"cas": 0}, "data": synthetic}),
            ("kv_get_namespace_a", "GET", path_a, None),
            ("kv_metadata_update_namespace_a", "POST", path_a.replace("/data/", "/metadata/", 1), {"max_versions": 2}),
            ("kv_metadata_get_namespace_a", "GET", path_a.replace("/data/", "/metadata/", 1), None),
            ("role_write_rendered_body_a", "POST", role_a, render.rehearsal_auth_role(NS_A)),
            ("role_write_rendered_body_b", "POST", role_b, render.rehearsal_auth_role(NS_B)),
            # The name does not decide the scope; the token's own namespace does (case in step 5).
            ("role_write_other_name_same_constraints", "POST", role_cross, render.rehearsal_auth_role(NS_A)),
            ("token_lookup_self", "GET", "auth/token/lookup-self", None),
            ("token_renew_self", "POST", "auth/token/renew-self", {}),
        ]
        for name, method, path, body in allowed:
            case(name, "allow", outcome(bao.call(method, path, writer, body)[0]))

        reader_role = render.rehearsal_auth_role(NS_A)

        def role(**changes):
            body = dict(reader_role)
            for key, value in changes.items():
                if value is None:
                    body.pop(key, None)
                else:
                    body[key] = value
            return body

        evil_body = {"policy": 'path "secret/data/bravo/target/*" { capabilities = ["read"] }\n'}
        denied = [
            # (1) No policy access at all, whatever the name.
            ("policy_write_operator_reader", "PUT", f"sys/policies/acl/{render.READER_POLICY}", evil_body),
            ("policy_read_operator_reader", "GET", f"sys/policies/acl/{render.READER_POLICY}", None),
            ("policy_delete_operator_reader", "DELETE", f"sys/policies/acl/{render.READER_POLICY}", None),
            ("policy_write_reader_prefixed", "PUT", f"sys/policies/acl/{render.rehearsal_role_name(NS_A)}", evil_body),
            ("policy_write_default", "PUT", "sys/policies/acl/default", {"policy": 'path "*" { capabilities = ["sudo"] }'}),
            ("policy_write_own_writer", "PUT", "sys/policies/acl/rehearsal-openbao-writer", {"policy": 'path "*" { capabilities = ["read"] }'}),
            ("policy_write_target_reader", "PUT", "sys/policies/acl/target-app-reader", evil_body),
            ("policy_write_legacy_endpoint", "PUT", "sys/policy/rehearsal-app-reader-legacy", evil_body),
            ("policy_list_not_granted", "LIST", "sys/policies/acl", None),
            ("policy_password_not_granted", "PUT", "sys/policies/password/rehearsal", {"policy": "length=8"}),
            # Paths outside the agreed subtree.
            ("kv_put_target", "POST", "secret/data/bravo/target/precutover/database", {"options": {"cas": 1}, "data": synthetic}),
            ("kv_get_target", "GET", "secret/data/bravo/target/precutover/database", None),
            ("kv_metadata_get_target", "GET", "secret/metadata/bravo/target/precutover/database", None),
            ("kv_metadata_delete_target", "DELETE", "secret/metadata/bravo/target/precutover/database", None),
            ("kv_put_sibling_prefix", "POST", "secret/data/bravo/rehearsal-evil/database", {"options": {"cas": 0}, "data": synthetic}),
            ("kv_destroy_rehearsal_not_granted", "POST", path_a.replace("/data/", "/destroy/", 1), {"versions": [1]}),
            ("kv_list_rehearsal_not_granted", "LIST", "secret/metadata/bravo/rehearsal/", None),
            ("role_write_own_writer", "POST", "auth/kubernetes/role/rehearsal-openbao-writer", role()),
            ("role_write_target_reader", "POST", "auth/kubernetes/role/target-app-reader", role()),
            ("role_write_wrong_accessor_check_role", "POST", "auth/kubernetes/role/t9-check-wrong-accessor", role()),
            ("role_read_blocked_by_required_parameters", "GET", role_a, None),
            # (1) token_policies is exactly the operator policy.
            ("role_policy_evil_name", "POST", role_a, role(token_policies="rehearsal-app-reader-evil")),
            ("role_policy_reader_prefixed_name", "POST", role_a, role(token_policies=render.rehearsal_role_name(NS_A))),
            ("role_policy_glob_value", "POST", role_a, role(token_policies="rehearsal-app-reader*")),
            ("role_policy_trailing_space", "POST", role_a, role(token_policies="rehearsal-app-reader ")),
            ("role_policy_comma_list", "POST", role_a, role(token_policies="rehearsal-app-reader,default")),
            ("role_policy_json_list", "POST", role_a, role(token_policies=["rehearsal-app-reader"])),
            ("role_policy_default", "POST", role_a, role(token_policies="default")),
            ("role_policy_writer", "POST", role_a, role(token_policies="rehearsal-openbao-writer")),
            ("role_policy_target_reader", "POST", role_a, role(token_policies="target-app-reader")),
            ("role_policy_missing", "POST", role_a, role(token_policies=None)),
            ("role_deprecated_policies_param", "POST", role_a, role(policies="default")),
            # (3) Namespace: rehearsal-* only, never the management namespace or the legacy
            #     rehearsal-control*, no lists or globs.
            ("role_ns_management_namespace", "POST", role_a, role(bound_service_account_namespaces=render.WRITER_NAMESPACE)),
            ("role_ns_target_dev", "POST", role_a, role(bound_service_account_namespaces="target-dev")),
            ("role_ns_bravo_secrets", "POST", role_a, role(bound_service_account_namespaces="bravo-secrets")),
            ("role_ns_default", "POST", role_a, role(bound_service_account_namespaces="default")),
            ("role_ns_kube_system", "POST", role_a, role(bound_service_account_namespaces="kube-system")),
            ("role_ns_legacy_control", "POST", role_a, role(bound_service_account_namespaces="rehearsal-control")),
            ("role_ns_legacy_control_suffix", "POST", role_a, role(bound_service_account_namespaces="rehearsal-control-x")),
            ("role_ns_legacy_control_trailing_space", "POST", role_a, role(bound_service_account_namespaces="rehearsal-control ")),
            ("role_ns_trailing_space", "POST", role_a, role(bound_service_account_namespaces=f"{NS_A} ")),
            ("role_ns_comma_list", "POST", role_a, role(bound_service_account_namespaces=f"{NS_A},target-dev")),
            ("role_ns_comma_two_rehearsal", "POST", role_a, role(bound_service_account_namespaces=f"{NS_A},{NS_B}")),
            ("role_ns_glob_value", "POST", role_a, role(bound_service_account_namespaces="rehearsal-*")),
            ("role_ns_star", "POST", role_a, role(bound_service_account_namespaces="*")),
            ("role_ns_json_list", "POST", role_a, role(bound_service_account_namespaces=[NS_A])),
            ("role_ns_selector_param", "POST", role_a, role(bound_service_account_namespace_selector='{"matchLabels":{}}')),
            ("role_ns_missing", "POST", role_a, role(bound_service_account_namespaces=None)),
            # Other fixed fields.
            ("role_sa_writer", "POST", role_a, role(bound_service_account_names=render.WRITER_ACCOUNT)),
            ("role_sa_comma_list", "POST", role_a, role(bound_service_account_names="rehearsal-eso-reader,rehearsal-openbao-writer")),
            ("role_sa_star", "POST", role_a, role(bound_service_account_names="*")),
            ("role_missing_audience", "POST", role_a, role(audience=None)),
            ("role_other_audience", "POST", role_a, role(audience="kubernetes")),
            ("role_long_ttl", "POST", role_a, role(token_ttl="768h")),
            ("role_long_max_ttl", "POST", role_a, role(token_max_ttl="768h")),
            ("role_default_policy_on", "POST", role_a, role(token_no_default_policy=False)),
            ("role_period_param", "POST", role_a, role(token_period="24h")),
            ("role_cidr_param", "POST", role_a, role(token_bound_cidrs="0.0.0.0/0")),
            ("role_alias_name_source_param", "POST", role_a, role(alias_name_source="serviceaccount_name")),
            ("sys_auth_enable", "POST", "sys/auth/other", {"type": "kubernetes"}),
            ("sys_auth_read", "GET", "sys/auth", None),
            ("sys_auth_kubernetes_read", "GET", "sys/auth/kubernetes", None),
            ("sys_mounts_enable", "POST", "sys/mounts/other", {"type": "kv"}),
            ("kubernetes_config_write", "POST", "auth/kubernetes/config", {"kubernetes_host": "https://example.invalid"}),
            ("token_create_child", "POST", "auth/token/create", {"policies": ["rehearsal-openbao-writer"]}),
            ("identity_entity_write", "POST", "identity/entity", {"name": "x"}),
            ("identity_entity_alias_write", "POST", "identity/entity-alias", {"name": "x", "mount_accessor": accessor}),
        ]
        for name, method, path, body in denied:
            case(name, "deny", outcome(bao.call(method, path, writer, body)[0]))
        # ``..`` is not resolved inside the ACL: the HTTP layer redirects to the cleaned path,
        # and following that redirect is an ordinary request for bravo/target.
        case("kv_get_traversal_not_served", "redirect", outcome(bao.call(
            "GET", "secret/data/bravo/rehearsal/../target/precutover/database", writer)[0]))
        cleaned = (bao.location or "").split("/v1/", 1)[-1]
        case("kv_get_traversal_redirect_target", "secret/data/bravo/target/precutover/database", cleaned)
        case("kv_get_traversal_followed", "deny", outcome(bao.call("GET", cleaned, writer)[0]))

        # 4. Previous escalation (16d8483 evidence), replayed: every step now fails.
        evil = "rehearsal-app-reader-evil"
        case("escalation_policy_body_grants_target", "deny",
             outcome(bao.call("PUT", f"sys/policies/acl/{evil}", writer, evil_body)[0]),
             "A안: the writer has no sys/policies access")
        case("escalation_bind_evil_policy_to_reader_sa", "deny", outcome(bao.call(
            "POST", f"auth/kubernetes/role/{evil}", writer, role(bound_service_account_namespaces=NS_B, token_policies=evil))[0]),
            "token_policies must be exactly rehearsal-app-reader")
        case("escalation_bind_evil_policy_to_writer_itself", "deny", outcome(bao.call(
            "POST", f"auth/kubernetes/role/{evil}", writer,
            role(bound_service_account_names=render.WRITER_ACCOUNT, bound_service_account_namespaces=render.WRITER_NAMESPACE,
                 token_policies=evil))[0]))
        holder = bao.login(evil, kube.token(NS_B, READER))
        case("escalation_reader_sa_login_evil_role", "deny", "allow" if holder else "deny")
        root_view = bao.call("GET", f"sys/policies/acl/{evil}", root)[0]
        case("escalation_evil_policy_absent", 404, root_view)

        # 5. (2) Readers: login only in the bound namespace; read only their own namespace path.
        reader_a = bao.login(render.rehearsal_role_name(NS_A), kube.token(NS_A, READER))
        reader_b = bao.login(render.rehearsal_role_name(NS_B), kube.token(NS_B, READER))
        case("reader_a_login_role_a", "allow", "allow" if reader_a else "deny")
        case("reader_b_login_role_b", "allow", "allow" if reader_b else "deny")
        case("reader_b_login_role_a", "deny", "allow" if bao.login(render.rehearsal_role_name(NS_A), kube.token(NS_B, READER)) else "deny")
        case("reader_a_login_role_b", "deny", "allow" if bao.login(render.rehearsal_role_name(NS_B), kube.token(NS_A, READER)) else "deny")
        case("reader_sa_in_control_login_role_a", "deny",
             "allow" if bao.login(render.rehearsal_role_name(NS_A), kube.token(render.WRITER_NAMESPACE, READER)) else "deny")
        case("reader_sa_in_target_dev_login_role_a", "deny",
             "allow" if bao.login(render.rehearsal_role_name(NS_A), kube.token("target-dev", READER)) else "deny")
        case("reader_a_default_audience", "deny",
             "allow" if bao.login(render.rehearsal_role_name(NS_A), kube.token(NS_A, READER, None)) else "deny")
        if reader_a:
            info = bao.call("GET", "auth/token/lookup-self", reader_a)[1].get("data", {})
            results["reader_token"] = {"policies": info.get("policies"), "ttl": info.get("ttl")}
            case("reader_a_token_policies", [render.READER_POLICY], info.get("policies"))
            case("reader_a_get_own_namespace", "allow", outcome(bao.call("GET", path_a, reader_a)[0]))
            case("reader_a_get_namespace_b", "deny", outcome(bao.call("GET", path_b, reader_a)[0]))
            case("reader_a_get_prefix_sibling_namespace", "deny", outcome(bao.call("GET", path_prefix, reader_a)[0]))
            case("reader_a_get_target", "deny", outcome(bao.call("GET", "secret/data/bravo/target/precutover/database", reader_a)[0]))
            case("reader_a_get_target_objects", "deny", outcome(bao.call("GET", "secret/data/bravo/target/objects", reader_a)[0]))
            case("reader_a_get_subtree_root", "deny", outcome(bao.call("GET", "secret/data/bravo/rehearsal/database", reader_a)[0]))
            case("reader_a_get_writer_namespace_path", "deny", outcome(bao.call(
                "GET", f"secret/data/bravo/rehearsal/{render.WRITER_NAMESPACE}/database", reader_a)[0]))
            case("reader_a_write_own_namespace", "deny", outcome(bao.call(
                "POST", path_a, reader_a, {"options": {"cas": 2}, "data": synthetic})[0]))
            case("reader_a_metadata_own_namespace", "deny", outcome(bao.call(
                "GET", path_a.replace("/data/", "/metadata/", 1), reader_a)[0]))
            case("reader_a_list_subtree", "deny", outcome(bao.call("LIST", "secret/metadata/bravo/rehearsal/", reader_a)[0]))
            case("reader_a_policy_read", "deny", outcome(bao.call("GET", f"sys/policies/acl/{render.READER_POLICY}", reader_a)[0]))
            case("reader_a_role_write", "deny", outcome(bao.call("POST", role_a, reader_a, reader_role)[0]))
            case("reader_a_token_create", "deny", outcome(bao.call("POST", "auth/token/create", reader_a, {})[0]))
        if reader_b:
            case("reader_b_get_own_namespace", "allow", outcome(bao.call("GET", path_b, reader_b)[0]))
            case("reader_b_get_namespace_a", "deny", outcome(bao.call("GET", path_a, reader_b)[0]))
            case("reader_b_get_target", "deny", outcome(bao.call("GET", "secret/data/bravo/target/precutover/database", reader_b)[0]))
        cross = bao.login("rehearsal-app-reader-crossname", kube.token(NS_A, READER))
        case("crossname_role_login_a", "allow", "allow" if cross else "deny")
        if cross:
            case("crossname_role_reads_own_namespace_only", "allow", outcome(bao.call("GET", path_a, cross)[0]))
            case("crossname_role_namespace_b", "deny", outcome(bao.call("GET", path_b, cross)[0]))
        wrong = bao.login("t9-check-wrong-accessor", kube.token(NS_A, READER))
        case("wrong_accessor_policy_login", "allow", "allow" if wrong else "deny", "root-made check role")
        if wrong:
            case("wrong_accessor_policy_fails_closed", "deny", outcome(bao.call("GET", path_a, wrong)[0]),
                 "template does not resolve, rule skipped")
        for token in (reader_a, reader_b, cross, wrong):
            if token:
                bao.call("POST", "auth/token/revoke-self", token, {})
        for path in ("auth/kubernetes/role/t9-check-wrong-accessor", "sys/policies/acl/t9-check-wrong-accessor"):
            bao.call("DELETE", path, root)

        # 6. Cleanup through the writer's own grants, then revoke.
        for name, method, path in [
            ("cleanup_role_delete_a", "DELETE", role_a), ("cleanup_role_delete_b", "DELETE", role_b),
            ("cleanup_role_delete_crossname", "DELETE", role_cross),
            ("cleanup_kv_data_delete_b", "DELETE", path_b),
            ("cleanup_kv_metadata_delete_a", "DELETE", path_a.replace("/data/", "/metadata/", 1)),
            ("cleanup_kv_metadata_delete_b", "DELETE", path_b.replace("/data/", "/metadata/", 1)),
            ("cleanup_kv_metadata_delete_prefix_sibling", "DELETE", path_prefix.replace("/data/", "/metadata/", 1)),
        ]:
            case(name, "allow", outcome(bao.call(method, path, writer)[0]))
        case("token_revoke_self", "allow", outcome(bao.call("POST", "auth/token/revoke-self", writer, {})[0]))
        case("revoked_token_unusable", "deny", outcome(bao.call("GET", "auth/token/lookup-self", writer)[0]))

        # 7. (4) Happy path through Pods: labelled writer Job prepares namespace A, the rendered
        #    init container in A fetches credentials.json, a second writer Job cleans up.
        kube.apply(writer_job("writer-prepare", WRITER_PREPARE, NS_A))
        kube.run("wait", "--for=condition=complete", "job/writer-prepare", "-n", render.WRITER_NAMESPACE, "--timeout=120s", check=False)
        logs = kube.run("logs", "job/writer-prepare", "-n", render.WRITER_NAMESPACE, check=False).stdout
        seen = dict(line.split()[1:3] for line in logs.splitlines() if line.startswith("RESULT "))
        for name, want in {"pod_login": "allow", "pod_kv_put_namespace_path": "allow", "pod_kv_get_namespace_path": "allow",
                           "pod_kv_get_target": "deny", "pod_policy_write_operator_reader": "deny",
                           "pod_policy_write_evil": "deny", "pod_reader_role_write": "allow",
                           "pod_revoke_self": "allow"}.items():
            case(name, want, seen.get(name, "missing"))

        # Without a reader ingress peer the rendered NetworkPolicy blocks the init container.
        kube.apply(reader_pod("reader-no-peer", NS_A, READER_POD_LABEL))
        phase, init_code = pod_phase(kube, NS_A, "reader-no-peer")
        results["network"]["reader_init_without_peer"] = {"phase": phase, "init_exit": init_code}
        case("reader_init_blocked_without_reader_peer", "Failed", phase, "rendered network policy has no rehearsal reader peer")
        kube.apply(test_only_reader_peer())
        time.sleep(3)
        kube.apply(reader_pod("reader-fetch", NS_A, READER_POD_LABEL))
        phase, init_code = pod_phase(kube, NS_A, "reader-fetch")
        results["network"]["reader_init_with_test_peer"] = {"phase": phase, "init_exit": init_code}
        case("reader_init_fetch_succeeds", "Succeeded", phase, "test-only reader ingress peer")
        app_logs = kube.run("logs", "reader-fetch", "-n", NS_A, "-c", "app", check=False).stdout
        app = {line.split()[1]: line.split()[2:] for line in app_logs.splitlines() if line.startswith("RESULT ")}
        case("reader_credentials_file_written", ["ok", "mode=400"], app.get("credentials_file", "missing"))
        case("reader_no_temp_file_left", ["0"], app.get("tmp_files", "missing"))

        kube.apply(writer_job("writer-cleanup", WRITER_CLEANUP, NS_A))
        kube.run("wait", "--for=condition=complete", "job/writer-cleanup", "-n", render.WRITER_NAMESPACE, "--timeout=120s", check=False)
        logs = kube.run("logs", "job/writer-cleanup", "-n", render.WRITER_NAMESPACE, check=False).stdout
        seen = dict(line.split()[1:3] for line in logs.splitlines() if line.startswith("RESULT "))
        for name in ("pod_cleanup_login", "pod_reader_role_delete", "pod_kv_metadata_delete", "pod_cleanup_revoke_self"):
            case(name, "allow", seen.get(name, "missing"))
        case("after_cleanup_role_absent", 404, bao.call("GET", role_a, root)[0])
        case("after_cleanup_kv_absent", 404, bao.call("GET", path_a, root)[0])
        case("after_cleanup_reader_login", "deny",
             "allow" if bao.login(render.rehearsal_role_name(NS_A), kube.token(NS_A, READER)) else "deny")
        case("after_cleanup_operator_policy_kept", 200, bao.call("GET", f"sys/policies/acl/{render.READER_POLICY}", root)[0])

        # 8. Real network path for the writer label (unchanged rendered policy).
        for name, namespace, labels, want in [
            ("np_writer_label_in_control", render.WRITER_NAMESPACE, render.WRITER_POD_LABEL, 0),
            ("np_unlabelled_in_control", render.WRITER_NAMESPACE, {}, "blocked"),
            ("np_writer_label_in_rehearsal_ns", NS_B, render.WRITER_POD_LABEL, "blocked"),
            ("np_writer_label_in_legacy_control", render.LEGACY_WRITER_NAMESPACE, render.WRITER_POD_LABEL, "blocked"),
        ]:
            kube.apply(probe_pod(name.replace("_", "-"), namespace, labels))
            code = pod_exit(kube, namespace, name.replace("_", "-"))
            actual = 0 if code == 0 else "blocked"
            results["network"][name] = code
            case(name, want, actual)
    finally:
        forward.terminate()
    results["init_substitutions"] = [list(pair) for pair in INIT_SUBSTITUTIONS]
    results["summary"] = {"total": len(results["cases"]), "failed": [c["name"] for c in results["cases"] if not c["pass"]]}
    Path(args.output).write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results["summary"]))
    return 1 if results["summary"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
