#!/usr/bin/env python3
"""Local-only check of the T15 rehearsal writer against a real OpenBao and Kubernetes.

Runs on a throwaway kind cluster (context ``kind-*`` only): an upstream OpenBao dev server
stands in for ``target-openbao``, the *rendered* NetworkPolicy, writer policy and roles are
applied unchanged, and real ServiceAccount tokens log in through Kubernetes TokenReview.
Prints and writes only case names and outcomes; tokens and synthetic values never leave memory.

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
READER_NS = "rehearsal-0123456789abcdef01234567"  # runner-style rehearsal-<24 hex>
OTHER_NS = "rehearsal-89abcdef0123456789abcdef"
RUN, OTHER_RUN = "run-a", "run-b"
RESTRICTED = {"allowPrivilegeEscalation": False, "runAsNonRoot": True, "capabilities": {"drop": ["ALL"]},
              "seccompProfile": {"type": "RuntimeDefault"}}
POD_SECURITY = {"runAsNonRoot": True, "runAsUser": 100, "runAsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"}}


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
                                     "env": [{"name": "BAO_ADDR", "value": "http://target-openbao.bravo-secrets.svc:8200"},
                                             {"name": "BAO_CLIENT_TIMEOUT", "value": "8s"}, {"name": "HOME", "value": "/tmp"}],
                                     "securityContext": RESTRICTED}]}}


JOB_SCRIPT = r"""
set -u
export BAO_ADDR=http://target-openbao.bravo-secrets.svc:8200 BAO_CLIENT_TIMEOUT=10s HOME=/tmp
r() { if "$@" >/dev/null 2>&1; then echo allow; else echo deny; fi; }
JWT=$(cat /var/run/secrets/openbao/token)
TOKEN=$(bao write -field=token auth/kubernetes/login role=rehearsal-openbao-writer jwt="$JWT" 2>/dev/null) || TOKEN=''
[ -n "$TOKEN" ] && echo "RESULT pod_login allow" || { echo "RESULT pod_login deny"; exit 1; }
export BAO_TOKEN="$TOKEN"
echo "RESULT pod_kv_put_rehearsal $(r bao kv put -mount=secret -cas=0 bravo/rehearsal/run-pod/database credentials_json=@/work/synthetic.json)"
echo "RESULT pod_kv_get_rehearsal $(r bao kv get -mount=secret bravo/rehearsal/run-pod/database)"
echo "RESULT pod_kv_get_target $(r bao kv get -mount=secret bravo/target/precutover/database)"
echo "RESULT pod_reader_policy_write $(r bao policy write rehearsal-app-reader-run-pod /work/reader.hcl)"
echo "RESULT pod_reader_role_write $(r sh -c 'bao write auth/kubernetes/role/rehearsal-app-reader-run-pod - < /work/role.json')"
echo "RESULT pod_reader_role_delete $(r bao delete auth/kubernetes/role/rehearsal-app-reader-run-pod)"
echo "RESULT pod_reader_policy_delete $(r bao policy delete rehearsal-app-reader-run-pod)"
echo "RESULT pod_kv_metadata_delete $(r bao kv metadata delete -mount=secret bravo/rehearsal/run-pod/database)"
echo "RESULT pod_revoke_self $(r bao token revoke -self)"
"""


def writer_job(reader_ns):
    work = {"synthetic.json": json.dumps({"username": "rehearsal", "password": "synthetic-" + secrets.token_hex(8)}),
            "reader.hcl": render.rehearsal_policy("run-pod"),
            "role.json": json.dumps(render.rehearsal_auth_role(reader_ns, "run-pod")),
            "job.sh": JOB_SCRIPT}
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "writer-check", "namespace": render.WRITER_NAMESPACE},
              "data": work}
    job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "writer-check", "namespace": render.WRITER_NAMESPACE},
           "spec": {"backoffLimit": 0, "template": {"metadata": {"labels": dict(render.WRITER_POD_LABEL)}, "spec": {
               "restartPolicy": "Never", "serviceAccountName": render.WRITER_ACCOUNT, "automountServiceAccountToken": False,
               "securityContext": POD_SECURITY,
               "containers": [{"name": "writer", "image": IMAGE, "command": ["sh", "/work/job.sh"], "securityContext": RESTRICTED,
                               "volumeMounts": [{"name": "work", "mountPath": "/work", "readOnly": True},
                                                {"name": "token", "mountPath": "/var/run/secrets/openbao", "readOnly": True}]}],
               "volumes": [{"name": "work", "configMap": {"name": "writer-check"}},
                           {"name": "token", "projected": {"sources": [{"serviceAccountToken": {
                               "audience": "openbao", "expirationSeconds": 600, "path": "token"}}]}}]}}}}
    return lst(config, job)


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
    results = {"cases": [], "dry_run": {}, "network": {}}

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
    kube.apply(lst(ns(READER_NS, **render.REHEARSAL_LABEL), ns(OTHER_NS, **render.REHEARSAL_LABEL)))
    for namespace in (READER_NS, OTHER_NS):
        kube.apply({"apiVersion": "v1", "kind": "ServiceAccount",
                    "metadata": {"name": "rehearsal-eso-reader", "namespace": namespace}})
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
        # 2. Operator setup as on the target (README step 8), plus the writer pieces.
        version = bao.call("GET", "sys/health")[1].get("version")
        results["openbao_version"] = version
        for method, path, body in [
            ("POST", "sys/auth/kubernetes", {"type": "kubernetes"}),
            ("POST", "auth/kubernetes/config", {"kubernetes_host": "https://kubernetes.default.svc"}),
            ("POST", "secret/config", {"cas_required": True}),
            ("PUT", "sys/policies/acl/rehearsal-openbao-writer", {"policy": render.rehearsal_writer_policy()}),
            ("POST", "auth/kubernetes/role/rehearsal-openbao-writer", render.rehearsal_writer_auth_role()),
            ("POST", "secret/data/bravo/target/precutover/database",
             {"options": {"cas": 0}, "data": {"credentials_json": "synthetic-" + secrets.token_hex(8)}}),
        ]:
            status, body_out = bao.call(method, path, root, body)
            if status >= 300:
                raise SystemExit(f"setup {path} failed: {status} {body_out.get('errors')}")

        # 3. Writer identity: only the exact SA, namespace and audience log in.
        writer = bao.login("rehearsal-openbao-writer", kube.token(render.WRITER_NAMESPACE, render.WRITER_ACCOUNT))
        case("writer_login_exact_sa_audience", "allow", "allow" if writer else "deny")
        case("writer_login_default_audience", "deny",
             "allow" if bao.login("rehearsal-openbao-writer", kube.token(render.WRITER_NAMESPACE, render.WRITER_ACCOUNT, None)) else "deny")
        case("writer_role_other_sa", "deny",
             "allow" if bao.login("rehearsal-openbao-writer", kube.token(READER_NS, "rehearsal-eso-reader")) else "deny")
        if not writer:
            raise SystemExit("writer login failed; cannot continue")
        lookup = bao.call("GET", "auth/token/lookup-self", writer)[1].get("data", {})
        results["writer_token"] = {"policies": lookup.get("policies"), "ttl": lookup.get("ttl")}
        case("writer_token_policies_only_writer", ["rehearsal-openbao-writer"], lookup.get("policies"))

        reader_role = render.rehearsal_auth_role(READER_NS, RUN)
        synthetic = {"credentials_json": json.dumps({"username": "rehearsal", "password": "synthetic-" + secrets.token_hex(8)})}
        allowed = [
            ("kv_put_rehearsal_run", "POST", f"secret/data/bravo/rehearsal/{RUN}/database", {"options": {"cas": 0}, "data": synthetic}),
            ("kv_put_rehearsal_other_run", "POST", f"secret/data/bravo/rehearsal/{OTHER_RUN}/database", {"options": {"cas": 0}, "data": synthetic}),
            ("kv_get_rehearsal", "GET", f"secret/data/bravo/rehearsal/{RUN}/database", None),
            ("kv_metadata_update_rehearsal", "POST", f"secret/metadata/bravo/rehearsal/{RUN}/database", {"max_versions": 2}),
            ("kv_metadata_get_rehearsal", "GET", f"secret/metadata/bravo/rehearsal/{RUN}/database", None),
            ("reader_policy_write", "PUT", f"sys/policies/acl/rehearsal-app-reader-{RUN}", {"policy": render.rehearsal_policy(RUN)}),
            ("reader_policy_write_other_run", "PUT", f"sys/policies/acl/rehearsal-app-reader-{OTHER_RUN}", {"policy": render.rehearsal_policy(OTHER_RUN)}),
            ("reader_policy_read", "GET", f"sys/policies/acl/rehearsal-app-reader-{RUN}", None),
            ("reader_role_write_rendered_body", "POST", f"auth/kubernetes/role/rehearsal-app-reader-{RUN}", reader_role),
            ("token_lookup_self", "GET", "auth/token/lookup-self", None),
            ("token_renew_self", "POST", "auth/token/renew-self", {}),
        ]
        for name, method, path, body in allowed:
            case(name, "allow", outcome(bao.call(method, path, writer, body)[0]))

        def role(**changes):
            body = dict(reader_role)
            for key, value in changes.items():
                if value is None:
                    body.pop(key, None)
                else:
                    body[key] = value
            return body

        role_path = f"auth/kubernetes/role/rehearsal-app-reader-{RUN}"
        denied = [
            ("kv_put_target", "POST", "secret/data/bravo/target/precutover/database", {"options": {"cas": 1}, "data": synthetic}),
            ("kv_get_target", "GET", "secret/data/bravo/target/precutover/database", None),
            ("kv_metadata_get_target", "GET", "secret/metadata/bravo/target/precutover/database", None),
            ("kv_metadata_delete_target", "DELETE", "secret/metadata/bravo/target/precutover/database", None),
            ("kv_put_sibling_prefix", "POST", "secret/data/bravo/rehearsal-evil/database", {"options": {"cas": 0}, "data": synthetic}),
            ("kv_destroy_rehearsal_not_granted", "POST", f"secret/destroy/bravo/rehearsal/{RUN}/database", {"versions": [1]}),
            ("kv_list_rehearsal_not_granted", "LIST", "secret/metadata/bravo/rehearsal/", None),
            ("policy_write_default", "PUT", "sys/policies/acl/default", {"policy": 'path "*" { capabilities = ["sudo"] }'}),
            ("policy_write_own_writer", "PUT", "sys/policies/acl/rehearsal-openbao-writer", {"policy": 'path "*" { capabilities = ["read"] }'}),
            ("policy_write_target_reader", "PUT", "sys/policies/acl/target-app-reader", {"policy": 'path "*" { capabilities = ["read"] }'}),
            ("policy_write_without_dash", "PUT", "sys/policies/acl/rehearsal-app-readerx", {"policy": 'path "*" { capabilities = ["read"] }'}),
            ("policy_write_legacy_endpoint", "PUT", "sys/policy/rehearsal-app-reader-legacy", {"policy": 'path "*" { capabilities = ["read"] }'}),
            ("policy_list_not_granted", "LIST", "sys/policies/acl", None),
            ("role_write_own_writer", "POST", "auth/kubernetes/role/rehearsal-openbao-writer", role()),
            ("role_write_target_reader", "POST", "auth/kubernetes/role/target-app-reader", role()),
            ("role_read_blocked_by_required_parameters", "GET", role_path, None),
            ("role_ns_target_dev", "POST", role_path, role(bound_service_account_namespaces="target-dev")),
            ("role_ns_writer_namespace", "POST", role_path, role(bound_service_account_namespaces="rehearsal-control")),
            ("role_ns_writer_namespace_trailing_space", "POST", role_path, role(bound_service_account_namespaces="rehearsal-control ")),
            ("role_ns_comma_list", "POST", role_path, role(bound_service_account_namespaces=f"{READER_NS},target-dev")),
            ("role_ns_glob_value", "POST", role_path, role(bound_service_account_namespaces="rehearsal-*")),
            ("role_ns_star", "POST", role_path, role(bound_service_account_namespaces="*")),
            ("role_ns_json_list", "POST", role_path, role(bound_service_account_namespaces=[READER_NS])),
            ("role_ns_selector_param", "POST", role_path, role(bound_service_account_namespace_selector='{"matchLabels":{}}')),
            ("role_sa_writer", "POST", role_path, role(bound_service_account_names=render.WRITER_ACCOUNT)),
            ("role_sa_comma_list", "POST", role_path, role(bound_service_account_names="rehearsal-eso-reader,rehearsal-openbao-writer")),
            ("role_sa_star", "POST", role_path, role(bound_service_account_names="*")),
            ("role_missing_audience", "POST", role_path, role(audience=None)),
            ("role_other_audience", "POST", role_path, role(audience="kubernetes")),
            ("role_long_ttl", "POST", role_path, role(token_ttl="768h")),
            ("role_long_max_ttl", "POST", role_path, role(token_max_ttl="768h")),
            ("role_default_policy_on", "POST", role_path, role(token_no_default_policy=False)),
            ("role_policy_default", "POST", role_path, role(token_policies="default")),
            ("role_policy_writer", "POST", role_path, role(token_policies="rehearsal-openbao-writer")),
            ("role_policy_comma_list", "POST", role_path, role(token_policies=f"rehearsal-app-reader-{RUN},default")),
            ("role_deprecated_policies_param", "POST", role_path, role(policies="default")),
            ("role_period_param", "POST", role_path, role(token_period="24h")),
            ("role_cidr_param", "POST", role_path, role(token_bound_cidrs="0.0.0.0/0")),
            ("sys_auth_enable", "POST", "sys/auth/other", {"type": "kubernetes"}),
            ("sys_auth_read", "GET", "sys/auth", None),
            ("sys_mounts_enable", "POST", "sys/mounts/other", {"type": "kv"}),
            ("kubernetes_config_write", "POST", "auth/kubernetes/config", {"kubernetes_host": "https://example.invalid"}),
            ("token_create_child", "POST", "auth/token/create", {"policies": ["rehearsal-openbao-writer"]}),
            ("identity_entity_write", "POST", "identity/entity", {"name": "x"}),
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

        # 4. The run's reader: exact namespace, this run's path only.
        reader = bao.login(f"rehearsal-app-reader-{RUN}", kube.token(READER_NS, "rehearsal-eso-reader"))
        case("reader_login_bound_namespace", "allow", "allow" if reader else "deny")
        case("reader_login_other_rehearsal_namespace", "deny",
             "allow" if bao.login(f"rehearsal-app-reader-{RUN}", kube.token(OTHER_NS, "rehearsal-eso-reader")) else "deny")
        if reader:
            case("reader_get_own_run", "allow", outcome(bao.call("GET", f"secret/data/bravo/rehearsal/{RUN}/database", reader)[0]))
            case("reader_get_other_run", "deny", outcome(bao.call("GET", f"secret/data/bravo/rehearsal/{OTHER_RUN}/database", reader)[0]))
            case("reader_get_target", "deny", outcome(bao.call("GET", "secret/data/bravo/target/precutover/database", reader)[0]))
            case("reader_write_own_run", "deny", outcome(bao.call(
                "POST", f"secret/data/bravo/rehearsal/{RUN}/database", reader, {"options": {"cas": 1}, "data": synthetic})[0]))

        # 5. Escalation: a reader-named policy whose body grants bravo/target, bound by a role
        #    that passes every parameter constraint. Recorded as observed, not assumed.
        evil = "rehearsal-app-reader-evil"
        evil_body = 'path "secret/data/bravo/target/*" { capabilities = ["read"] }\n'
        case("escalation_policy_body_grants_target", "allow",
             outcome(bao.call("PUT", f"sys/policies/acl/{evil}", writer, {"policy": evil_body})[0]),
             "residual risk: the ACL cannot inspect the policy body")
        case("escalation_bind_evil_policy_to_writer_itself", "deny", outcome(bao.call(
            "POST", f"auth/kubernetes/role/{evil}", writer,
            role(bound_service_account_names=render.WRITER_ACCOUNT, bound_service_account_namespaces=render.WRITER_NAMESPACE,
                 token_policies=evil))[0]))
        case("escalation_bind_evil_policy_to_reader_sa", "allow", outcome(bao.call(
            "POST", f"auth/kubernetes/role/{evil}", writer, role(bound_service_account_namespaces=OTHER_NS, token_policies=evil))[0]),
            "allowed by every parameter constraint")
        holder = bao.login(evil, kube.token(OTHER_NS, "rehearsal-eso-reader"))
        status = bao.call("GET", "secret/data/bravo/target/precutover/database", holder)[0] if holder else 403
        case("escalation_reader_sa_token_reads_target", "allow", outcome(status),
             "needs a token for rehearsal-eso-reader in a rehearsal-* namespace, which the writer SA alone cannot mint")
        writer_self = bao.login(evil, kube.token(render.WRITER_NAMESPACE, render.WRITER_ACCOUNT))
        case("escalation_writer_sa_logs_into_evil_role", "deny", "allow" if writer_self else "deny")

        # 6. Cleanup through the writer's own grants, then revoke.
        for name, method, path in [
            ("cleanup_role_delete", "DELETE", role_path), ("cleanup_evil_role_delete", "DELETE", f"auth/kubernetes/role/{evil}"),
            ("cleanup_policy_delete", "DELETE", f"sys/policies/acl/rehearsal-app-reader-{RUN}"),
            ("cleanup_other_policy_delete", "DELETE", f"sys/policies/acl/rehearsal-app-reader-{OTHER_RUN}"),
            ("cleanup_evil_policy_delete", "DELETE", f"sys/policies/acl/{evil}"),
            ("cleanup_kv_data_delete", "DELETE", f"secret/data/bravo/rehearsal/{OTHER_RUN}/database"),
            ("cleanup_kv_metadata_delete", "DELETE", f"secret/metadata/bravo/rehearsal/{RUN}/database"),
            ("cleanup_kv_metadata_delete_other", "DELETE", f"secret/metadata/bravo/rehearsal/{OTHER_RUN}/database"),
        ]:
            case(name, "allow", outcome(bao.call(method, path, writer)[0]))
        case("token_revoke_self", "allow", outcome(bao.call("POST", "auth/token/revoke-self", writer, {})[0]))
        case("revoked_token_unusable", "deny", outcome(bao.call("GET", "auth/token/lookup-self", writer)[0]))

        # 7. Real network path: the labelled writer Job in rehearsal-control, then negatives.
        kube.apply(writer_job(READER_NS))
        kube.run("wait", "--for=condition=complete", "job/writer-check", "-n", render.WRITER_NAMESPACE, "--timeout=120s", check=False)
        logs = kube.run("logs", "job/writer-check", "-n", render.WRITER_NAMESPACE, check=False).stdout
        expected = {"pod_login": "allow", "pod_kv_put_rehearsal": "allow", "pod_kv_get_rehearsal": "allow",
                    "pod_kv_get_target": "deny", "pod_reader_policy_write": "allow", "pod_reader_role_write": "allow",
                    "pod_reader_role_delete": "allow", "pod_reader_policy_delete": "allow",
                    "pod_kv_metadata_delete": "allow", "pod_revoke_self": "allow"}
        seen = dict(line.split()[1:3] for line in logs.splitlines() if line.startswith("RESULT "))
        for name, want in expected.items():
            case(name, want, seen.get(name, "missing"))
        for name, namespace, labels, want in [
            ("np_writer_label_in_control", render.WRITER_NAMESPACE, render.WRITER_POD_LABEL, 0),
            ("np_unlabelled_in_control", render.WRITER_NAMESPACE, {}, "blocked"),
            ("np_writer_label_in_rehearsal_ns", READER_NS, render.WRITER_POD_LABEL, "blocked"),
        ]:
            kube.apply(probe_pod(name.replace("_", "-"), namespace, labels))
            code = pod_exit(kube, namespace, name.replace("_", "-"))
            actual = 0 if code == 0 else "blocked"
            results["network"][name] = code
            case(name, want, actual)
    finally:
        forward.terminate()
    results["summary"] = {"total": len(results["cases"]), "failed": [c["name"] for c in results["cases"] if not c["pass"]]}
    Path(args.output).write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results["summary"]))
    return 1 if results["summary"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
