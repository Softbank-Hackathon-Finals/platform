#!/usr/bin/env python3
"""Run the rendered target app on a throwaway local k3s and record what it does.

Real: the renderers' output, the demo-chat source, CNPG, PostgreSQL, PgBouncer and a
restricted namespace. Stand-ins: see STAND_INS. Every run has its own names and removes only
the container and image it created, after checking their ID and run label. Generated
credentials stay in memory and reach kubectl through stdin.

    infra/envs/target-workloads/testing/app_lab.py --demo-chat-src <checkout> --output <file>
"""
import argparse
import base64
import json
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
WORKLOADS = ROOT / "infra/envs/target-workloads"
RUN_ID = uuid.uuid4().hex[:12]
RUN_LABEL = "t30-app-run"
K3S = f"t30-app-k3s-{RUN_ID}"
OWNED = {}
NODE = "t30-app-node"
K3S_IMAGE = "mirror.gcr.io/rancher/k3s:v1.35.5-k3s1"
LOCAL_IMAGE = "t30-app-lab/demo-chat"
REPOSITORY = "ghcr.io/softbank-hackathon-finals/demo-chat"
CNPG_MANIFEST = "https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v1.30.1/cnpg-1.30.1.yaml"
NS, SOURCE_NS = "target-dev", "source-dev"
SOURCE_HOST = "host=source-db-rw.source-dev.svc.cluster.local"
TARGET_HOST = "host=target-db-rw.target-dev.svc.cluster.local"
POOLER_DB_HOST = "target-pgbouncer.target-dev.svc.cluster.local"
TARGET_DB_HOST = "target-db-rw.target-dev.svc.cluster.local"
STAND_INS = {
    "cluster": "single-node k3s in Docker, not RKE2 on the development VM",
    "image": "local arm64 build of the demo-chat source, not the published GHCR bytes",
    "source_db": "CNPG source-db in the same cluster, not EKS",
    "tailscale": "ClusterIP of source-db-rw given to pooler.py as the egress Service; no tailnet",
    "t9_secrets": "generated values in the T9 Secret names; no OpenBao or ESO",
    "operator_install": "official 1.30.1 release manifest, not the Helm chart with its namespace scope",
    "object_store": "absent; attachments were not exercised",
    "edge": "absent; requests came from a Pod through the Service DNS name",
}
CLIENT = r"""
import json, time, urllib.error, urllib.request
BASE = "http://demo-chat.target-dev.svc.cluster.local:8080"
out = {}
for key, method, path, body in REQUESTS:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"} if data else {}
    started = time.monotonic()
    try:
        response = urllib.request.urlopen(urllib.request.Request(BASE + path, data, headers, method=method), timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    except OSError as error:
        out[key] = {"connected": False, "error": type(error).__name__}
        continue
    with response:
        payload = response.read(65536)
    result = {"status": response.status, "x_bravo_env": response.headers.get_all("X-Bravo-Env"),
              "retry_after": response.headers.get("Retry-After"),
              "seconds": round(time.monotonic() - started, 2)}
    if path == "/api/info":
        result["info"] = json.loads(payload)
    out[key] = result
print(json.dumps(out))
"""
LOGIN = r"""
import psycopg
try:
    with psycopg.connect(host="target-pgbouncer.target-dev.svc.cluster.local", dbname="chat", user="chat_app",
                         password=PASSWORD, sslmode="require", connect_timeout=10, prepare_threshold=None) as connection:
        connection.execute("select 1")
    print("ok")
except psycopg.Error as error:
    text = str(error)
    if "wrong password type" in text:
        print("pooler_has_no_scram_keys")
    elif "password authentication failed" in text or "SASL authentication failed" in text:
        print("password_rejected")
    else:
        print("other")
"""
SET_BLOCK = ("import json,os,sys,urllib.request as u;"
             "print(u.urlopen(u.Request('http://127.0.0.1:8080/admin/write-block',"
             "json.dumps({'blocked':sys.argv[1]=='true'}).encode(),"
             "{'Authorization':'Bearer '+os.environ['ADMIN_TOKEN'],'Content-Type':'application/json'},"
             "method='PUT'),timeout=10).status)")


def run(*command, stdin=None, check=True):
    result = subprocess.run(command, input=stdin, capture_output=True, check=False)
    if check and result.returncode:
        # Only generated local values could appear in this tail, and only on a failed lab run.
        raise RuntimeError(f"failed: {command[:8]} rc={result.returncode}: "
                           + result.stderr.decode(errors="replace")[-400:].replace("\n", " "))
    return result.stdout.decode(errors="replace")


def kubectl(*args, stdin=None, check=True):
    return run("docker", "exec", "-i", K3S, "kubectl", *args, stdin=stdin, check=check)


def wait(condition, what, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(2)
    raise TimeoutError(what)


def render(script, *args):
    # Run the operators' command, so its CLI and output are what the lab applies.
    return json.loads(run(sys.executable, str(WORKLOADS / script), *args))["items"]


def apply(items):
    kubectl("apply", "--server-side", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": items}).encode())


def replace_secret(namespace, name, data, secret_type="Opaque"):
    kubectl("-n", namespace, "delete", "secret", name, "--ignore-not-found")
    # create, not apply: client-side apply would keep the value in an annotation.
    kubectl("create", "-f", "-", stdin=json.dumps({
        "apiVersion": "v1", "kind": "Secret", "type": secret_type,
        "metadata": {"name": name, "namespace": namespace, "labels": {"component": "target"}},
        "stringData": data}).encode())


def psql(namespace, pod, sql):
    return kubectl("-n", namespace, "exec", pod, "-c", "postgres", "--", "psql", "-d", "chat", "-tAc", sql).strip()


def secret_value(namespace, name, key):
    data = json.loads(kubectl("-n", namespace, "get", "secret", name, "-o", "json"))["data"]
    return base64.b64decode(data[key]).decode()


def start_cluster():
    OWNED["container"] = run(
        "docker", "run", "-d", "--name", K3S, "--hostname", NODE, "--label", f"{RUN_LABEL}={RUN_ID}", "--privileged",
        "--tmpfs", "/run", "--tmpfs", "/var/run", K3S_IMAGE, "server",
        "--disable", "traefik", "--disable", "servicelb", "--disable", "metrics-server").strip()
    wait(lambda: " Ready " in kubectl("get", "node", NODE, "--no-headers", check=False), "node Ready")
    run("docker", "exec", K3S, "sh", "-ec",
        "mkdir -p /var/lib/bravo-target-dev/postgres; chown 26:26 /var/lib/bravo-target-dev/postgres; "
        "chmod 700 /var/lib/bravo-target-dev/postgres")


def local_image_tag(short_revision):
    return f"{LOCAL_IMAGE}:{short_revision}-{RUN_ID}"


def remove_owned():
    """Remove what this run created, and only after its ID and run label still match at this moment."""
    result = {"container": "none_created", "image": "none_created"}
    container = OWNED.get("container")
    if container:
        seen = run("docker", "inspect", "--format", '{{.Id}} {{index .Config.Labels "' + RUN_LABEL + '"}}',
                   container, check=False).split()
        result["container"] = "not_owned_left_in_place"
        if seen == [container, RUN_ID]:
            run("docker", "rm", "-fv", container, check=False)
            result["container"] = "removed"
    image = OWNED.get("image")
    if image:
        label = run("docker", "image", "inspect", "--format", '{{index .Config.Labels "' + RUN_LABEL + '"}}',
                    image, check=False).strip()
        result["image"] = "not_owned_left_in_place"
        if label == RUN_ID:
            run("docker", "rmi", image, check=False)
            result["image"] = "removed"
    return result


def inject_local_image(items, image):
    """Test manifest only. The general render path refuses any digest but the registered one."""
    items = json.loads(json.dumps(items))
    for item in items:
        if item["kind"] == "Deployment":
            pod = item["spec"]["template"]["spec"]
            for container in [*pod["containers"], *pod.get("initContainers", [])]:
                container["image"] = image
    return items


def node_architecture():
    # The local build runs on this node, so the lab renders for its architecture, as operators do.
    return kubectl("get", "node", NODE, "-o", "jsonpath={.status.nodeInfo.architecture}").strip()


def general_path_accepts(image):
    result = subprocess.run([sys.executable, str(WORKLOADS / "application.py"), "--image", image],
                            capture_output=True, check=False)
    return result.returncode == 0


def import_image(source):
    revision = run("git", "-C", str(source), "rev-parse", "HEAD").strip()
    local = local_image_tag(revision[:7])
    # Same base digest from a public mirror, so an expired Docker Hub login cannot stop the build.
    dockerfile = (source / "Dockerfile").read_text().replace("FROM python:", "FROM mirror.gcr.io/library/python:")
    with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile") as temporary:
        temporary.write(dockerfile)
        temporary.flush()
        run("docker", "build", "-q", "-f", temporary.name, "--label", f"{RUN_LABEL}={RUN_ID}", "-t", local, str(source))
    OWNED["image"] = local
    archive = subprocess.run(["docker", "save", local], capture_output=True, check=True).stdout
    run("docker", "exec", "-i", K3S, "ctr", "-n", "k8s.io", "images", "import", "-", stdin=archive)
    listed = run("docker", "exec", K3S, "ctr", "-n", "k8s.io", "images", "ls", f"name==docker.io/{local}")
    digest = re.search(r"sha256:[0-9a-f]{64}", listed).group()
    image = f"{REPOSITORY}@{digest}"
    # The same alias step as docs/operations/target-image-handoff.md.
    run("docker", "exec", K3S, "ctr", "-n", "k8s.io", "images", "tag", f"docker.io/{local}", image)
    return image, revision


def install_databases():
    with urllib.request.urlopen(CNPG_MANIFEST, timeout=60) as response:
        kubectl("apply", "--server-side", "-f", "-", stdin=response.read())
    kubectl("-n", "cnpg-system", "rollout", "status", "deployment/cnpg-controller-manager", "--timeout=240s")

    target = render("render.py", "database", "--node-name", NODE)
    apply([item for item in target if item["kind"] == "Namespace"])
    source = json.loads(run(sys.executable, str(ROOT / "infra/envs/source-workloads/render.py"), "database"))["items"]
    for item in source:
        if item["kind"] == "Cluster":
            item["spec"]["storage"]["storageClass"] = "local-path"
    source = [item for item in source if item["kind"] != "StorageClass"]
    apply([item for item in source if item["kind"] == "Namespace"])

    passwords = {}
    for namespace, name, user in ((NS, "target-app-db", "chat_app"), (NS, "target-replication", "chat_replicator"),
                                  (SOURCE_NS, "source-app-db", "chat_app"), (SOURCE_NS, "source-replication", "chat_replicator")):
        passwords[name] = secrets.token_urlsafe(24)
        replace_secret(namespace, name, {"username": user, "password": passwords[name]}, "kubernetes.io/basic-auth")
    for items in (target, source):
        apply([item for item in items if item["kind"] != "Namespace"])
    for namespace, name in ((NS, "target-db"), (SOURCE_NS, "source-db")):
        wait(lambda n=namespace, c=name: kubectl("-n", n, "get", f"cluster.postgresql.cnpg.io/{c}", check=False), f"{name} exists", 60)
        kubectl("-n", namespace, "wait", "--for=condition=Ready", f"cluster.postgresql.cnpg.io/{name}", "--timeout=420s")
    apply(render("render.py", "migration"))
    kubectl("-n", NS, "wait", "--for=condition=complete", "job/target-chat-schema-v1", "--timeout=180s")
    return passwords


def pooler_backend(backend, passwords):
    """Point the rendered pooler at source (pre-cutover) or target (the NOLOGIN case)."""
    namespace, pod, ca, new, old = (
        (SOURCE_NS, "source-db-1", "source-db-ca", SOURCE_HOST, TARGET_HOST) if backend == "source"
        else (NS, "target-db-1", "target-db-ca", TARGET_HOST, SOURCE_HOST))
    verifier = psql(namespace, pod, "select rolpassword from pg_authid where rolname='chat_app'")
    replace_secret(NS, "target-pgbouncer-auth", {"userlist.txt": f'"chat_app" "{verifier}"\n'})
    replace_secret(NS, "t8-source-ca", {"ca.crt": secret_value(namespace, ca, "ca.crt")})
    config = json.loads(kubectl("-n", NS, "get", "configmap", "target-pgbouncer-config", "-o", "json", check=False) or "{}")
    if config:
        ini = config["data"]["pgbouncer.ini"].replace(old, new)
        kubectl("-n", NS, "patch", "configmap", "target-pgbouncer-config", "--type=merge", "-p",
                json.dumps({"data": {"pgbouncer.ini": ini}}))
        kubectl("-n", NS, "rollout", "restart", "deployment/target-pgbouncer")
        kubectl("-n", NS, "rollout", "status", "deployment/target-pgbouncer", "--timeout=180s")
    password = passwords["source-app-db" if backend == "source" else "target-app-db"]
    replace_secret(NS, "target-app-credentials", {"credentials.json": json.dumps({"username": "chat_app", "password": password})})


def install_pooler(passwords):
    pooler_backend("source", passwords)
    service = json.loads(kubectl("-n", SOURCE_NS, "get", "service", "source-db-rw", "-o", "json"))
    apply(render("pooler.py", "--egress-address", service["spec"]["clusterIP"],
                 "--egress-service-uid", service["metadata"]["uid"]))
    kubectl("-n", NS, "rollout", "status", "deployment/target-pgbouncer", "--timeout=180s")


def start_client(image):
    apply([{"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "t30-lab-client", "namespace": NS, "labels": {"component": "target"}},
            "spec": {"automountServiceAccountToken": False, "restartPolicy": "Never",
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{"name": "client", "image": image, "imagePullPolicy": "Never",
                                     "command": ["/app/.venv/bin/python", "-c", "import time; time.sleep(7200)"],
                                     "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                         "capabilities": {"drop": ["ALL"]}}}]}}])
    kubectl("-n", NS, "wait", "--for=condition=Ready", "pod/t30-lab-client", "--timeout=120s")


def client(requests):
    script = f"REQUESTS = {requests!r}\n{CLIENT}"
    return json.loads(kubectl("-n", NS, "exec", "-i", "t30-lab-client", "--", "/app/.venv/bin/python", "-", stdin=script.encode()))


def endpoints():
    slices = json.loads(kubectl("-n", NS, "get", "endpointslices", "-l", "kubernetes.io/service-name=demo-chat", "-o", "json"))
    return sum(1 for item in slices["items"] for ep in item.get("endpoints") or [] if ep.get("conditions", {}).get("ready"))


def app_pods():
    return json.loads(kubectl("-n", NS, "get", "pods", "-l", "app=demo-chat", "-o", "json"))["items"]


def statuses(pod, kind):
    return {s["name"]: s for s in pod.get("status", {}).get(kind) or []}


def set_block(blocked):
    return kubectl("-n", NS, "exec", "deployment/demo-chat", "-c", "app", "--", "python", "-c", SET_BLOCK, str(blocked).lower()).strip()


def counts():
    return {"source_messages": int(psql(SOURCE_NS, "source-db-1", "select count(*) from messages")),
            "target_messages": int(psql(NS, "target-db-1", "select count(*) from messages")),
            "target_chat_app_can_login": psql(NS, "target-db-1", "select rolcanlogin from pg_roles where rolname='chat_app'") == "t"}


def message(key):
    return {"idempotency_key": key, "author": "t30 lab", "body": "synthetic"}


READS = [("healthz", "GET", "/healthz", None), ("readyz", "GET", "/readyz", None), ("info", "GET", "/api/info", None),
         ("health_db", "GET", "/api/health/db", None), ("messages", "GET", "/api/messages", None)]
KEY_A, KEY_B = "00000000-0000-4000-8000-00000000000a", "00000000-0000-4000-8000-00000000000b"


def live_pod():
    return next(p for p in app_pods() if not p["metadata"].get("deletionTimestamp"))


def app_log_count(pattern):
    return kubectl("-n", NS, "logs", live_pod()["metadata"]["name"], "-c", "app").count(pattern)


def target_db_log_count(pattern):
    return kubectl("-n", NS, "logs", "target-db-1", "-c", "postgres").count(pattern)


def scenario_closed(passwords, manifest):
    """Secret missing or malformed, or Secrets Manager unreachable: nothing may serve."""
    out = {}
    for name, values in (("target-app-objects", {"access_key": secrets.token_urlsafe(12), "secret_key": secrets.token_urlsafe(24)}),
                         ("target-app-admin", {"token": secrets.token_urlsafe(32)})):
        replace_secret(NS, name, values)
    kubectl("-n", NS, "delete", "secret", "target-app-credentials", "--ignore-not-found")
    apply(manifest)
    time.sleep(20)
    pod = live_pod()
    out["secret_missing"] = {"ready_endpoints": endpoints(), "pod_phase": pod["status"]["phase"],
                             "app_started": statuses(pod, "containerStatuses").get("app", {}).get("started", False)}
    replace_secret(NS, "target-app-credentials", {"credentials.json": "not-json"})
    kubectl("-n", NS, "delete", "pods", "-l", "app=demo-chat", "--wait=false")

    def sidecar_failed():
        pods = [p for p in app_pods() if not p["metadata"].get("deletionTimestamp")]
        side = pods and statuses(pods[0], "initContainerStatuses").get("secrets-manager-compat", {})
        return pods[0] if side and side.get("restartCount", 0) >= 1 else None

    pod = wait(sidecar_failed, "sidecar failure on malformed credentials", 120)
    out["secret_malformed"] = {"ready_endpoints": endpoints(),
                               "sidecar_restarts": statuses(pod, "initContainerStatuses")["secrets-manager-compat"]["restartCount"],
                               "app_started": statuses(pod, "containerStatuses").get("app", {}).get("started", False)}

    pooler_backend("source", passwords)
    # Control for question 1: the same image with the answer unreachable and no AWS identity.
    kubectl("-n", NS, "scale", "deployment/demo-chat", "--replicas=0")
    wait(lambda: not app_pods(), "no app Pod", 120)
    kubectl("-n", NS, "set", "env", "deployment/demo-chat", "-c", "app", "AWS_ENDPOINT_URL_SECRETS_MANAGER=http://127.0.0.1:1")
    kubectl("-n", NS, "scale", "deployment/demo-chat", "--replicas=1")

    def app_failed():
        for pod in app_pods():
            if pod["metadata"].get("deletionTimestamp"):
                continue
            state = statuses(pod, "containerStatuses").get("app", {})
            if state.get("restartCount", 0) >= 1:
                return pod
        return None

    pod = wait(app_failed, "app failure without Secrets Manager", 180)
    logs = kubectl("-n", NS, "logs", pod["metadata"]["name"], "-c", "app", "--previous")
    out["secrets_manager_unreachable"] = {
        "app_restarts": statuses(pod, "containerStatuses")["app"]["restartCount"], "ready": statuses(pod, "containerStatuses")["app"]["ready"],
        "ready_endpoints": endpoints(), "application_startup_failed": "Application startup failed" in logs,
        "endpoint_connection_error": "EndpointConnectionError" in logs}
    kubectl("-n", NS, "set", "env", "deployment/demo-chat", "-c", "app", "AWS_ENDPOINT_URL_SECRETS_MANAGER=http://127.0.0.1:18081")
    kubectl("-n", NS, "rollout", "status", "deployment/demo-chat", "--timeout=180s")
    return out


def switch_route(backend, db_host, passwords):
    """Stop the app first. Its old password must not reach the restarted pooler (see scenario_first_login)."""
    kubectl("-n", NS, "scale", "deployment/demo-chat", "--replicas=0")
    wait(lambda: not app_pods(), "no app Pod", 120)
    pooler_backend(backend, passwords)
    kubectl("-n", NS, "set", "env", "deployment/demo-chat", "-c", "app", f"DB_HOST={db_host}")
    kubectl("-n", NS, "scale", "deployment/demo-chat", "--replicas=1")
    kubectl("-n", NS, "rollout", "status", "deployment/demo-chat", "--timeout=180s")
    wait(lambda: len(app_pods()) == 1 and endpoints() == 1, "one ready app")


def scenario_nologin(passwords, via):
    """Correct password, closed role: what the app and the Service report."""
    db_log_before = target_db_log_count("not permitted to log in")
    switch_route("target", POOLER_DB_HOST if via == "pooler" else TARGET_DB_HOST, passwords)
    time.sleep(8)
    out = {"before": counts(), "ready_endpoints": endpoints(), "reads": client(READS)}
    out["post_while_blocked"] = client([("post", "POST", "/api/messages", message(KEY_A))])["post"]
    out["unblock_status"] = set_block(False)
    out["post_after_unblock"] = client([("post", "POST", "/api/messages", message(KEY_A))])["post"]
    out["reblock_status"] = set_block(True)
    out["app_log_not_permitted_lines"] = app_log_count("not permitted to log in")
    out["target_db_log_not_permitted_lines"] = target_db_log_count("not permitted to log in") - db_log_before
    out["after"] = counts()
    return out


def login(password):
    """One login through the pooler; the password travels on stdin and only a category comes back."""
    script = f"PASSWORD = {password!r}\n{LOGIN}"
    return kubectl("-n", NS, "exec", "-i", "t30-lab-client", "--", "/app/.venv/bin/python", "-", stdin=script.encode()).strip()


def restart_pooler():
    kubectl("-n", NS, "rollout", "restart", "deployment/target-pgbouncer")
    kubectl("-n", NS, "rollout", "status", "deployment/target-pgbouncer", "--timeout=180s")
    time.sleep(3)


def scenario_first_login(passwords):
    """PgBouncer 1.25.1 keeps SCRAM keys only from the first login attempt after it starts."""
    kubectl("-n", NS, "scale", "deployment/demo-chat", "--replicas=0")
    wait(lambda: not app_pods(), "no app Pod", 120)
    pooler_backend("source", passwords)
    right = passwords["source-app-db"]
    wrong = secrets.token_urlsafe(24)
    out = {"wrong_first": {"first_wrong": login(wrong), "then_right": login(right)}}
    time.sleep(4)  # Longer than server_login_retry, so this is a new server login and not its back-off.
    out["wrong_first"]["right_again"] = login(right)
    restart_pooler()
    out["right_first"] = {"first_right": login(right), "then_wrong": login(wrong), "then_right": login(right)}
    return out


def scenario_precutover(passwords):
    switch_route("source", POOLER_DB_HOST, passwords)
    started = time.monotonic()
    # Readiness ignores the database, so report how long the path took instead of hiding it.
    wait(lambda: client([("db", "GET", "/api/health/db", None)])["db"].get("status") == 200, "db path", 240)
    db_path_seconds = round(time.monotonic() - started, 1)
    out = {"db_path_open_seconds_after_ready": db_path_seconds, "before": counts(), "ready_endpoints": endpoints(), "reads": client(READS)}
    out["upload_refused"] = client([("upload", "PUT", f"/api/attachments/{KEY_B}", None)])["upload"]
    out["post_while_blocked"] = client([("post", "POST", "/api/messages", message(KEY_B))])["post"]
    out["unblock_status"] = set_block(False)
    out["post_after_unblock"] = client([("post", "POST", "/api/messages", message(KEY_B))])["post"]
    out["same_key_retry"] = client([("post", "POST", "/api/messages", message(KEY_B))])["post"]
    out["read_back"] = client([("messages", "GET", "/api/messages", None)])["messages"]
    out["after"] = counts()
    kubectl("-n", NS, "rollout", "restart", "deployment/demo-chat")
    kubectl("-n", NS, "rollout", "status", "deployment/demo-chat", "--timeout=180s")
    started = time.monotonic()
    burst = client([(f"r{i}", "GET", "/api/info", None) for i in range(12)])
    out["requests_right_after_rollout"] = len(burst)
    out["failed_right_after_rollout"] = sum(1 for result in burst.values() if result.get("status") != 200)
    out["unlabelled_right_after_rollout"] = sum(1 for result in burst.values() if result.get("x_bravo_env") != ["onprem"])
    # The retiring Pod keeps answering during preStop, with the switch it already had.
    out["open_switch_answers_right_after_rollout"] = sum(
        1 for result in burst.values() if result.get("info", {}).get("write_blocked") is False)
    wait(lambda: len(app_pods()) == 1, "old Pod gone", 120)
    out["old_pod_gone_seconds_after_rollout"] = round(time.monotonic() - started, 1)
    out["after_restart"] = client([("info", "GET", "/api/info", None)])["info"].get("info")
    return out


def security_review():
    live = json.loads(kubectl("-n", NS, "get", "deployment", "demo-chat", "-o", "json"))["spec"]["template"]["spec"]
    labels = json.loads(kubectl("get", "namespace", NS, "-o", "json"))["metadata"]["labels"]
    default_exec = kubectl("-n", NS, "exec", live_pod()["metadata"]["name"], "--", "python", "-c",
                           "import os; print('ADMIN_TOKEN' in os.environ)").strip()
    return {"live_app_has_command": "command" in live["containers"][0],
            "exec_without_container_reaches_app": default_exec == "True",
            "namespace_pod_security": labels["pod-security.kubernetes.io/enforce"]}


def evaluate(report):
    """Every expected observation, named. A missing key or a wrong value is a failure."""
    closed, direct, pooled, pre = (report["closed_when_unusable"], report["nologin_direct"],
                                   report["nologin_via_pooler"], report["precutover_via_pooler_to_source"])
    checks = {
        "restricted_namespace": report["security"]["namespace_pod_security"] == "restricted",
        "general_render_path_refuses_local_build": report["general_path_refuses_local_build"],
        "app_keeps_image_entrypoint": report["security"]["live_app_has_command"] is False,
        "exec_without_container_reaches_app": report["security"]["exec_without_container_reaches_app"],
        "missing_secret_serves_nothing": closed["secret_missing"]["ready_endpoints"] == 0 and not closed["secret_missing"]["app_started"],
        "malformed_secret_stops_sidecar_before_app": closed["secret_malformed"]["ready_endpoints"] == 0
            and closed["secret_malformed"]["sidecar_restarts"] >= 1 and not closed["secret_malformed"]["app_started"],
        "image_fails_without_secrets_manager": closed["secrets_manager_unreachable"]["app_restarts"] >= 1
            and closed["secrets_manager_unreachable"]["ready_endpoints"] == 0
            and closed["secrets_manager_unreachable"]["application_startup_failed"]
            and closed["secrets_manager_unreachable"]["endpoint_connection_error"],
    }
    for name, case in (("direct", direct), ("pooler", pooled)):
        reads = case["reads"]
        checks.update({
            f"nologin_{name}_pod_stays_ready": case["ready_endpoints"] == 1 and reads["readyz"]["status"] == 200,
            f"nologin_{name}_business_reports_503": reads["health_db"]["status"] == 503 and reads["messages"]["status"] == 503
                and reads["messages"]["retry_after"] == "2",
            f"nologin_{name}_write_blocked_then_503": case["post_while_blocked"]["status"] == 503 and case["post_after_unblock"]["status"] == 503,
            f"nologin_{name}_no_row_anywhere": case["before"]["target_messages"] == case["after"]["target_messages"] == 0
                and case["before"]["source_messages"] == case["after"]["source_messages"],
            f"nologin_{name}_role_stays_closed": case["after"]["target_chat_app_can_login"] is False,
            f"nologin_{name}_refused_by_the_role_check": case["app_log_not_permitted_lines"] > 0
                and case["target_db_log_not_permitted_lines"] > 0,
            f"nologin_{name}_every_response_labelled": all(r["x_bravo_env"] == ["onprem"] for r in
                [*reads.values(), case["post_while_blocked"], case["post_after_unblock"]]),
        })
    responses = [*pre["reads"].values(), pre["upload_refused"], pre["post_while_blocked"], pre["post_after_unblock"],
                 pre["same_key_retry"], pre["read_back"]]
    closed_info = {"env": "onprem", "write_blocked": True, "attachment_upload_enabled": False}
    first = report["pooler_first_login"]
    checks.update({
        "pooler_wrong_first_login_blocks_correct_logins": first["wrong_first"] == {
            "first_wrong": "password_rejected", "then_right": "pooler_has_no_scram_keys", "right_again": "pooler_has_no_scram_keys"},
        "pooler_restart_and_right_first_login_recovers": first["right_first"] == {
            "first_right": "ok", "then_wrong": "password_rejected", "then_right": "ok"},
        "precutover_db_path_works": pre["reads"]["health_db"]["status"] == 200 and pre["reads"]["messages"]["status"] == 200,
        "precutover_starts_write_blocked": pre["reads"]["info"]["info"] == closed_info,
        "attachment_upload_refused_before_cutover": pre["upload_refused"]["status"] == 403,
        "precutover_blocked_post_is_503": pre["post_while_blocked"]["status"] == 503,
        "precutover_opened_post_commits_once": pre["post_after_unblock"]["status"] == 201 and pre["same_key_retry"]["status"] == 200
            and pre["after"]["source_messages"] == pre["before"]["source_messages"] + 1,
        "single_writer_target_db_untouched": pre["after"]["target_messages"] == 0,
        "target_role_stays_nologin": pre["after"]["target_chat_app_can_login"] is False,
        "every_response_labelled_onprem": all(r["x_bravo_env"] == ["onprem"] for r in responses),
        "restart_returns_to_write_blocked": pre["after_restart"] == closed_info,
        "rollout_is_not_held_by_the_sidecar": pre["old_pod_gone_seconds_after_rollout"] < 20,
        "rollout_drops_no_request": pre["failed_right_after_rollout"] == 0,
        "rollout_responses_stay_labelled": pre["unlabelled_right_after_rollout"] == 0,
    })
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-chat-src", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--keep", action="store_true", help="leave the k3s container for inspection")
    args = parser.parse_args()
    report = {"schema_version": 1, "stand_ins": STAND_INS, "kubernetes": K3S_IMAGE, "passed": False,
              "platform_revision": run("git", "-C", str(ROOT), "rev-parse", "HEAD").strip(),
              "platform_tree_clean": not run("git", "-C", str(ROOT), "status", "--porcelain").strip()}
    try:
        start_cluster()
        image, revision = import_image(args.demo_chat_src)
        pin = json.loads((WORKLOADS / "app-image.json").read_text())
        report.update(demo_chat_revision=revision, local_image_digest=image.split("@")[1],
                      pinned_source_tested=revision == pin["source_sha"])
        passwords = install_databases()
        install_pooler(passwords)
        start_client(image)
        report["general_path_refuses_local_build"] = not general_path_accepts(image)
        report["node_architecture"] = node_architecture()
        manifest = inject_local_image(render("application.py", "--arch", report["node_architecture"]), image)
        kubectl("apply", "--server-side", "--dry-run=server", "-f", "-",
                stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": manifest}).encode())
        report["restricted_server_dry_run"] = "accepted"
        report["closed_when_unusable"] = scenario_closed(passwords, manifest)
        report["security"] = security_review()
        report["nologin_direct"] = scenario_nologin(passwords, "direct")
        report["nologin_via_pooler"] = scenario_nologin(passwords, "pooler")
        report["pooler_first_login"] = scenario_first_login(passwords)
        report["precutover_via_pooler_to_source"] = scenario_precutover(passwords)
        report["checks"] = evaluate(report)
        report["passed"] = all(report["checks"].values())
    finally:
        report["cleanup"] = {"kept": dict(OWNED)} if args.keep else remove_owned()
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
