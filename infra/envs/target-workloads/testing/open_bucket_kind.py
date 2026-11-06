#!/usr/bin/env python3
"""Local-only measurement of the T9 demo exposure states (``closed`` ↔ ``open-demo``) on a real SeaweedFS.

Runs on a throwaway kind cluster (context ``kind-*``, one node) that maps NodePort 30833 to the host's
127.0.0.1:18333 (``--print-kind-config``). The repo's own renderers are applied unchanged: namespace and
StorageClass from ``render.py``, ServiceAccounts, and PV/PVC/Service/Deployment from ``objects.py`` with the
pinned SeaweedFS 4.52 index digest and the ``target-seaweedfs-auth`` Secret mounted read-only. The two
``objects.py`` NetworkPolicies are *not* applied so the host can reach 8333 through a test-only NodePort.

Each ``s3.json`` comes from ``object_exposure.render`` and is applied the way ``object_exposure.py plan``
says (Secret replaced in place). The run measures, with real unsigned and SigV4-signed HTTP requests:
whether the store picks the new Secret up without a restart, how long ``rollout restart`` takes to change
the observed state, what anonymous callers can and cannot do in ``open-demo``, whether a leftover bucket
policy keeps the bucket open after ``closed``, and whether ``object_exposure.py probe`` agrees each time.
A last informational step sends SIGHUP to the running ``weed`` process to see whether that alone re-reads
``s3.json``; the store is then returned to ``closed`` by the documented path.

    kind create cluster --name t9-open --kubeconfig <tmp> --config <(open_bucket_kind.py --print-kind-config)
    open_bucket_kind.py --kubeconfig <tmp> --output <private dir>/results.json
    kind delete cluster --name t9-open

Credentials are random per run and stay in memory (they reach the cluster only inside the Secret). Output
holds case names, HTTP statuses, S3 error codes, timings and hashes; never keys or object bodies.
This is not the demo RKE2 target.
"""
import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from string import Template

HERE = Path(__file__).resolve().parent
WORKLOADS = HERE.parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(WORKLOADS))

import object_exposure
import objects
import render

NAMESPACE = render.NAMESPACE
BUCKET = "demo-chat-attachments"
OTHER_BUCKET = "rehearsal-attachments"  # T15's rehearsal bucket; its identity is rendered by object_exposure
KEY = "t9/open-bucket-probe.txt"
OTHER_KEY = "t9/other-bucket-probe.txt"
SECRET_NAME = "target-seaweedfs-auth"
DEPLOYMENT = "target-seaweedfs"
MOUNTED = "/etc/seaweedfs-auth/s3.json"
NODE_PORT, HOST_PORT = 30833, 18333
ENDPOINT = f"http://127.0.0.1:{HOST_PORT}"
DEMO_RUN_ID = "t9-open-bucket-kind"
POLL = 2.0
NO_RESTART_AFTER_FILE = 90.0  # keep polling this long after the mounted file already shows the new bytes
FILE_WINDOW = 180.0  # kubelet's projected Secret refresh is periodic; give up waiting for the file after this
CHANGE_WINDOW = 180.0
MEASURED_SOURCES = ("infra/envs/target-workloads/testing/open_bucket_kind.py",
                    "infra/envs/target-workloads/object_exposure.py",
                    "infra/envs/target-workloads/objects.py", "infra/envs/target-workloads/render.py")
KIND_CONFIG = f"""kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
  extraPortMappings:
  - containerPort: {NODE_PORT}
    hostPort: {HOST_PORT}
    listenAddress: 127.0.0.1
    protocol: TCP
"""


class Kube:
    def __init__(self, kubeconfig):
        self.base = ["kubectl", "--kubeconfig", kubeconfig]

    def run(self, *args, stdin=None, check=True, timeout=300):
        result = subprocess.run(self.base + list(args), input=stdin, capture_output=True, text=True,
                                check=False, timeout=timeout)
        if check and result.returncode != 0:
            raise SystemExit(f"kubectl {args[0]} failed: {result.stderr.strip()[:300]}")
        return result

    def apply(self, doc):
        return self.run("apply", "-f", "-", stdin=json.dumps(doc)).stdout.strip()

    def json(self, *args):
        return json.loads(self.run(*args, "-o", "json").stdout)


def guard(kube):
    context = kube.run("config", "current-context").stdout.strip()
    nodes = kube.json("get", "nodes")["items"]
    if not context.startswith("kind-") or len(nodes) != 1 or not nodes[0]["metadata"]["name"].endswith("-control-plane"):
        raise SystemExit("refusing: this check runs only on a single-node local kind cluster")
    if kube.run("get", "namespace", NAMESPACE, check=False).returncode == 0:
        raise SystemExit(f"refusing: namespace {NAMESPACE} already exists; use a fresh kind cluster")
    return nodes[0]


def _hmac(key, text):
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect is recorded as its own status


_OPENER = urllib.request.build_opener(_NoRedirect())


def s3(method, path, *, query=(), body=b"", cred=None, timeout=10.0):
    """One path-style S3 request. ``cred`` (access, secret) signs it with SigV4 header auth, otherwise
    it is sent unsigned. Returns status (or ``UNKNOWN:<error>``), the S3 error code, and the body."""
    canonical_query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
                               for k, v in sorted(query))
    quoted = urllib.parse.quote(path, safe="/-_.~")
    headers = {}
    if cred:
        amz_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        payload = hashlib.sha256(body).hexdigest()
        signed_headers = {"host": f"127.0.0.1:{HOST_PORT}", "x-amz-content-sha256": payload, "x-amz-date": amz_date}
        names = ";".join(sorted(signed_headers))
        canonical = "\n".join([method, quoted, canonical_query,
                               *(f"{k}:{signed_headers[k]}" for k in sorted(signed_headers)), "", names, payload])
        scope = f"{amz_date[:8]}/us-east-1/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
        key = _hmac(_hmac(_hmac(_hmac(("AWS4" + cred[1]).encode(), amz_date[:8]), "us-east-1"), "s3"), "aws4_request")
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers = {"Authorization": f"AWS4-HMAC-SHA256 Credential={cred[0]}/{scope}, SignedHeaders={names}, "
                                    f"Signature={signature}",
                   "x-amz-content-sha256": payload, "x-amz-date": amz_date}
    url = ENDPOINT + quoted + (f"?{canonical_query}" if canonical_query else "")
    request = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None, method=method,
                                     headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, None, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        raw = error.read(1 << 16)
        code = None
        if b"<Code>" in raw:
            code = raw.split(b"<Code>", 1)[1].split(b"</Code>", 1)[0].decode(errors="replace")[:64]
        return error.code, code, b""
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as error:
        # A restarting store can drop or cut a response short (RemoteDisconnected, BadStatusLine).
        return f"UNKNOWN:{type(error).__name__}", None, b""


def outcome(status):
    if isinstance(status, str):
        return status
    return "ALLOWED" if 200 <= status < 300 else "DENIED" if status in (401, 403) else f"OTHER:{status}"


def anon_get(bucket=BUCKET, key=KEY):
    return outcome(s3("GET", f"/{bucket}/{key}")[0])


def probe_cli():
    """``object_exposure.py probe`` as an operator would run it, from the host."""
    result = subprocess.run([sys.executable, str(WORKLOADS / "object_exposure.py"), "probe", "--endpoint", ENDPOINT,
                             "--bucket", BUCKET, "--key", KEY], capture_output=True, text=True, check=False, timeout=30)
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {"error": "no JSON output", "exit": result.returncode}


def secret_doc(data):
    """Same object ``kubectl create secret generic --from-file=s3.json=... --dry-run=client -o json`` emits."""
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": SECRET_NAME, "namespace": NAMESPACE},
            "data": {"s3.json": base64.b64encode(data).decode()}}


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig")
    parser.add_argument("--output", help="private results file outside the repository (0600, not overwritten)")
    parser.add_argument("--print-kind-config", action="store_true", help="print the kind cluster config and exit")
    args = parser.parse_args()
    if args.print_kind_config:
        print(KIND_CONFIG, end="")
        return 0
    if not args.kubeconfig or not args.output:
        parser.error("--kubeconfig and --output are required")
    output = Path(args.output)
    if output.exists():
        raise SystemExit("refusing to overwrite an existing results file")
    kube = Kube(args.kubeconfig)
    node = guard(kube)
    node_name = node["metadata"]["name"]
    started = datetime.now().astimezone()
    clock = time.monotonic()
    results = {
        "started_at": started.isoformat(timespec="seconds"),
        "source": {"revision": git("rev-parse", "HEAD"),
                   "uncommitted_measured_sources": git("status", "--porcelain", "--", *MEASURED_SOURCES).splitlines(),
                   "sha256": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in MEASURED_SOURCES}},
        "environment": {}, "s3_json_sha256": {}, "transitions": {}, "cases": [], "probe_cli": {}, "events": [],
    }

    def elapsed():
        return round(time.monotonic() - clock, 1)

    def event(name, **fields):
        results["events"].append({"t": elapsed(), "event": name, **fields})
        print(f"[{elapsed():7.1f}s] {name} {json.dumps(fields) if fields else ''}", flush=True)

    def case(name, expected, actual, note=""):
        ok = actual == expected
        results["cases"].append({"name": name, "expected": expected, "actual": actual, "pass": ok,
                                 **({"note": note} if note else {})})
        print(f"{'PASS' if ok else 'FAIL'} {name}: expected={expected} actual={actual}", flush=True)

    def probe_case(label, expected_state):
        observed = probe_cli()
        results["probe_cli"][label] = observed
        case(f"probe_cli_{label}", expected_state, observed.get("state_observed"))

    # Random per-run identities; ``render`` validates them exactly as the operator path does.
    def credential():
        return secrets.token_hex(10).upper(), secrets.token_urlsafe(30)

    creds = {name: credential() for name in ("bootstrap", "client", "rehearsal")}
    environ = {}
    for name, (access_var, secret_var) in {**object_exposure.ENV, "rehearsal": object_exposure.REHEARSAL_ENV}.items():
        environ[access_var], environ[secret_var] = creds[name]
    rendered = {}
    for state in object_exposure.STATES:
        doc = object_exposure.render(state, BUCKET, environ, acknowledge_open=state == "open-demo",
                                     demo_run_id=DEMO_RUN_ID if state == "open-demo" else "",
                                     rehearsal_bucket=OTHER_BUCKET)
        data = (json.dumps(doc, indent=2) + "\n").encode()  # the bytes ``render --output`` writes
        rendered[state] = data
        info = object_exposure.summary(doc, state, BUCKET, data, DEMO_RUN_ID if state == "open-demo" else "")
        results["s3_json_sha256"][state] = info["s3_json_sha256"]
        results.setdefault("rendered", {})[state] = {"identities": info["identities"],
                                                     "anonymous_actions": info["anonymous_actions"]}
    bootstrap, client, rehearsal = creds["bootstrap"], creds["client"], creds["rehearsal"]

    def mounted_sha():
        out = kube.run("exec", "-n", NAMESPACE, f"deploy/{DEPLOYMENT}", "--", "sha256sum", MOUNTED,
                       check=False, timeout=30).stdout.split()
        return out[0] if out else None

    def pod_identity():
        pods = kube.json("get", "pods", "-n", NAMESPACE, "-l", "app.kubernetes.io/name=target-seaweedfs")["items"]
        running = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        if len(running) != 1:
            return {"pods": len(running)}
        status = (running[0]["status"].get("containerStatuses") or [{}])[0]
        return {"uid": running[0]["metadata"]["uid"], "restarts": status.get("restartCount"),
                "image_id": status.get("imageID")}

    def wait_rollout():
        kube.run("rollout", "status", "-n", NAMESPACE, f"deployment/{DEPLOYMENT}", "--timeout=300s", timeout=320)

    def until(predicate, window):
        start = time.monotonic()
        while True:
            value = predicate()
            if value or time.monotonic() - start >= window:
                return value, round(time.monotonic() - start, 1)
            time.sleep(POLL)

    def switch(label, state, want):
        """Replace the Secret, poll without restarting, then restart if the state has not changed."""
        target_sha = results["s3_json_sha256"][state]
        before = pod_identity()
        t0 = time.monotonic()
        applied_at = datetime.now().astimezone().isoformat(timespec="seconds")
        kube.apply(secret_doc(rendered[state]))
        samples, file_at, change_at = [], None, None
        while True:
            now = round(time.monotonic() - t0, 1)
            anon, mounted = anon_get(), mounted_sha()
            samples.append({"t": now, "anonymous_get": anon, "mounted_file_is_new": mounted == target_sha})
            if mounted == target_sha and file_at is None:
                file_at = now
            if anon == want:
                change_at = now
                break
            if (file_at is not None and now - file_at >= NO_RESTART_AFTER_FILE) or now >= FILE_WINDOW + NO_RESTART_AFTER_FILE:
                break
            time.sleep(POLL)
        after = pod_identity()
        record = {"secret_applied_at": applied_at, "target_s3_json_sha256": target_sha,
                  "no_restart": {"mounted_file_updated_after_s": file_at, "state_changed_after_s": change_at,
                                 "polled_for_s": samples[-1]["t"], "polls": len(samples),
                                 "pod_unchanged": before == after, "last_anonymous_get": samples[-1]["anonymous_get"]},
                  "samples": samples}
        event(f"{label}_no_restart", file_s=file_at, changed_s=change_at, polled_s=samples[-1]["t"])
        if change_at is None:
            t1 = time.monotonic()
            kube.run("rollout", "restart", "-n", NAMESPACE, f"deployment/{DEPLOYMENT}")
            wait_rollout()
            rollout_s = round(time.monotonic() - t1, 1)
            reached, _ = until(lambda: anon_get() == want, CHANGE_WINDOW)
            record["restart"] = {"rollout_status_done_after_s": rollout_s,
                                 "state_changed_after_s": round(time.monotonic() - t1, 1) if reached else None,
                                 "pod_replaced": pod_identity().get("uid") != before.get("uid")}
            event(f"{label}_restart", **record["restart"])
        record["restart_needed"] = change_at is None
        results["transitions"][label] = record
        case(f"{label}_state_reached", want, anon_get())

    try:
        # 1. Install the rendered target object store in its production shape (closed).
        namespace, storage = render.database(node_name)[:2]
        kube.apply({"apiVersion": "v1", "kind": "List", "items": [namespace, storage, *render.accounts()]})
        subprocess.run(["docker", "exec", node_name, "sh", "-c",
                        "mkdir -p /var/lib/bravo-target-dev/objects && chown 1000:1000 /var/lib/bravo-target-dev/objects"],
                       check=True, capture_output=True, timeout=60)
        kube.apply(secret_doc(rendered["closed"]))
        rendered_objects = objects.objects(node_name)
        results["not_applied"] = [f"{o['kind']}/{o['metadata']['name']}" for o in rendered_objects
                                  if o["kind"] == "NetworkPolicy"]
        kube.apply({"apiVersion": "v1", "kind": "List",
                    "items": [o for o in rendered_objects if o["kind"] != "NetworkPolicy"]})
        kube.apply({"apiVersion": "v1", "kind": "Service",
                    "metadata": {"name": "seaweedfs-t9-nodeport", "namespace": NAMESPACE, "labels": {"test-only": "t9"}},
                    "spec": {"type": "NodePort", "selector": objects.SELECTOR,
                             "ports": [{"port": 8333, "targetPort": "s3", "nodePort": NODE_PORT}]}})
        wait_rollout()
        ready, waited = until(lambda: s3("PUT", f"/{BUCKET}", cred=bootstrap)[0] in (200, 409), CHANGE_WINDOW)
        if not ready:
            raise SystemExit("SeaweedFS S3 did not accept the bucket creation")
        event("store_ready", waited_s=waited)
        body = b"t9 open-bucket probe " + secrets.token_hex(16).encode()
        other = b"t9 other-bucket probe " + secrets.token_hex(16).encode()
        put_ok, _ = until(lambda: s3("PUT", f"/{BUCKET}/{KEY}", body=body, cred=bootstrap)[0] == 200, 120)
        case("setup_bootstrap_put_object", True, bool(put_ok))
        case("setup_rehearsal_create_other_bucket", "ALLOWED", outcome(s3("PUT", f"/{OTHER_BUCKET}", cred=rehearsal)[0]))
        other_ok, _ = until(lambda: s3("PUT", f"/{OTHER_BUCKET}/{OTHER_KEY}", body=other, cred=rehearsal)[0] == 200, 120)
        case("setup_rehearsal_put_other_object", True, bool(other_ok))
        image_id = pod_identity().get("image_id")
        results["environment"] = {
            "kubernetes_server": kube.json("version")["serverVersion"]["gitVersion"],
            "node_os": node["status"]["nodeInfo"]["osImage"], "node_arch": node["status"]["nodeInfo"]["architecture"],
            "container_runtime": node["status"]["nodeInfo"]["containerRuntimeVersion"],
            "seaweedfs_image": objects.IMAGE, "seaweedfs_image_id": image_id,
            "network_policy": "objects.py NetworkPolicies not applied; host reaches 8333 via test-only NodePort",
        }
        results["objects"] = {"probe_object_sha256": hashlib.sha256(body).hexdigest(),
                              "other_object_sha256": hashlib.sha256(other).hexdigest()}

        def client_get(bucket=BUCKET, key=KEY, cred=client, expect=body):
            status, _, got = s3("GET", f"/{bucket}/{key}", cred=cred)
            return "ALLOWED_SAME_BYTES" if status == 200 and got == expect else outcome(status)

        def policy_status():
            status, code, _ = s3("GET", f"/{BUCKET}", query=[("policy", "")], cred=bootstrap)
            return f"{status}:{code}" if code else str(status)

        # 2. Closed (installed) state.
        case("closed_anonymous_get", "DENIED", anon_get())
        case("closed_client_get", "ALLOWED_SAME_BYTES", client_get())
        case("closed_anonymous_get_other_bucket", "DENIED", anon_get(OTHER_BUCKET, OTHER_KEY))
        case("closed_bucket_policy_absent", "404:NoSuchBucketPolicy", policy_status())
        probe_case("closed_initial", "closed")

        # 3. closed → open-demo.
        switch("open", "open-demo", "ALLOWED")
        case("open_anonymous_get", "ALLOWED", anon_get())
        anon_body = s3("GET", f"/{BUCKET}/{KEY}")[2]
        case("open_anonymous_get_same_bytes", True, anon_body == body)
        case("open_anonymous_head", "ALLOWED", outcome(s3("HEAD", f"/{BUCKET}/{KEY}")[0]))
        case("open_anonymous_put", "DENIED", outcome(s3("PUT", f"/{BUCKET}/t9/anonymous-put.txt", body=b"x")[0]))
        case("open_anonymous_put_absent", "OTHER:404", client_get(key="t9/anonymous-put.txt", expect=b"x"))
        case("open_anonymous_list_v2", "DENIED", outcome(s3("GET", f"/{BUCKET}", query=[("list-type", "2")])[0]))
        case("open_anonymous_list_v1", "DENIED", outcome(s3("GET", f"/{BUCKET}")[0]))
        case("open_anonymous_delete", "DENIED", outcome(s3("DELETE", f"/{BUCKET}/{KEY}")[0]))
        case("open_object_survives_anonymous_delete", "ALLOWED_SAME_BYTES", client_get())
        case("open_anonymous_get_other_bucket", "DENIED", anon_get(OTHER_BUCKET, OTHER_KEY))
        case("open_anonymous_list_other_bucket", "DENIED", outcome(s3("GET", f"/{OTHER_BUCKET}")[0]))
        policy = json.dumps({"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
            "Resource": Template("arn:aws:s3:::${bucket}/*").substitute(bucket=BUCKET)}]}).encode()
        case("open_anonymous_put_bucket_policy", "DENIED",
             outcome(s3("PUT", f"/{BUCKET}", query=[("policy", "")], body=policy)[0]))
        case("open_anonymous_create_bucket", "DENIED", outcome(s3("PUT", "/t9-anonymous-bucket")[0]))
        anon_list_all = s3("GET", "/")
        case("open_anonymous_list_buckets_no_names", True,
             BUCKET.encode() not in anon_list_all[2] and OTHER_BUCKET.encode() not in anon_list_all[2],
             f"status {anon_list_all[0]}")
        case("open_client_get", "ALLOWED_SAME_BYTES", client_get())
        case("open_client_put", "ALLOWED", outcome(s3("PUT", f"/{BUCKET}/t9/client-put.txt", body=b"c", cred=client)[0]))
        case("open_bucket_policy_absent", "404:NoSuchBucketPolicy", policy_status())
        probe_case("open_demo", "open")

        # 4. open-demo → closed.
        switch("close", "closed", "DENIED")
        case("closed_again_anonymous_get", "DENIED", anon_get())
        case("closed_again_client_get", "ALLOWED_SAME_BYTES", client_get())
        probe_case("closed_again", "closed")

        # 5. A leftover public bucket policy against a closed s3.json (T31's ANONYMOUS_READ_STILL_ALLOWED).
        put_policy = s3("PUT", f"/{BUCKET}", query=[("policy", "")], body=policy, cred=bootstrap)[0]
        case("leftover_policy_put_by_bootstrap", "ALLOWED", outcome(put_policy))
        t0 = time.monotonic()
        opened, _ = until(lambda: anon_get() == "ALLOWED", 30)
        results["transitions"]["leftover_policy_put"] = {"anonymous_allowed_after_s": round(time.monotonic() - t0, 1) if opened else None}
        case("leftover_policy_anonymous_get_with_closed_s3json", "ALLOWED", anon_get(),
             "closed s3.json alone does not close the bucket")
        case("leftover_policy_mounted_file_is_closed", results["s3_json_sha256"]["closed"], mounted_sha())
        probe_case("leftover_policy", "open")
        t1 = time.monotonic()
        kube.run("rollout", "restart", "-n", NAMESPACE, f"deployment/{DEPLOYMENT}")
        wait_rollout()
        until(lambda: not anon_get().startswith("UNKNOWN"), CHANGE_WINDOW)
        results["transitions"]["leftover_policy_restart"] = {"rollout_and_answer_after_s": round(time.monotonic() - t1, 1)}
        case("leftover_policy_survives_restart", "ALLOWED", anon_get())
        case("leftover_policy_anonymous_list_still_denied", "DENIED", outcome(s3("GET", f"/{BUCKET}")[0]))
        case("leftover_policy_anonymous_get_other_bucket", "DENIED", anon_get(OTHER_BUCKET, OTHER_KEY))
        case("leftover_policy_present", "200", policy_status())
        delete_policy = s3("DELETE", f"/{BUCKET}", query=[("policy", "")], cred=bootstrap)[0]
        case("leftover_policy_delete_by_bootstrap", "ALLOWED", outcome(delete_policy))
        t2 = time.monotonic()
        closed, _ = until(lambda: anon_get() == "DENIED", 30)
        results["transitions"]["leftover_policy_delete"] = {"anonymous_denied_after_s": round(time.monotonic() - t2, 1) if closed else None}
        case("after_policy_delete_anonymous_get", "DENIED", anon_get())
        case("after_policy_delete_client_get", "ALLOWED_SAME_BYTES", client_get())
        case("after_policy_delete_policy_absent", "404:NoSuchBucketPolicy", policy_status())
        probe_case("after_policy_delete", "closed")
        case("client_cannot_put_bucket_policy", "DENIED",
             outcome(s3("PUT", f"/{BUCKET}", query=[("policy", "")], body=policy, cred=client)[0]))
        case("client_cannot_delete_bucket_policy", "DENIED",
             outcome(s3("DELETE", f"/{BUCKET}", query=[("policy", "")], cred=client)[0]))

        # 6. Informational: does SIGHUP to the running weed process re-read s3.json (no Pod restart)?
        #    Not an operator step (it needs pods/exec); recorded so the restart requirement is understood.
        kube.apply(secret_doc(rendered["open-demo"]))
        target = results["s3_json_sha256"]["open-demo"]
        before = pod_identity()
        t0 = time.monotonic()
        updated, _ = until(lambda: mounted_sha() == target, FILE_WINDOW)
        file_s = round(time.monotonic() - t0, 1)
        still_closed = anon_get()
        hup = kube.run("exec", "-n", NAMESPACE, f"deploy/{DEPLOYMENT}", "--", "kill", "-HUP", "1",
                       check=False, timeout=30).returncode
        t1 = time.monotonic()
        reopened, _ = until(lambda: anon_get() == "ALLOWED", 20)
        results["transitions"]["sighup"] = {
            "mounted_file_updated_after_s": file_s if updated else None, "anonymous_before_hup": still_closed,
            "kill_exit": hup, "anonymous_allowed_after_hup_s": round(time.monotonic() - t1, 1) if reopened else None,
            "pod_unchanged": pod_identity() == before}
        event("sighup", **results["transitions"]["sighup"])
        # Leave the store closed by the documented path: closed Secret, rollout restart.
        kube.apply(secret_doc(rendered["closed"]))
        until(lambda: mounted_sha() == results["s3_json_sha256"]["closed"], FILE_WINDOW)
        kube.run("rollout", "restart", "-n", NAMESPACE, f"deployment/{DEPLOYMENT}")
        wait_rollout()
        until(lambda: anon_get() == "DENIED", CHANGE_WINDOW)
        case("final_anonymous_get", "DENIED", anon_get())
        case("final_client_get", "ALLOWED_SAME_BYTES", client_get())
        probe_case("final", "closed")
    finally:
        finished = datetime.now().astimezone()
        results["finished_at"] = finished.isoformat(timespec="seconds")
        results["duration_s"] = elapsed()
        results["summary"] = {"total": len(results["cases"]),
                              "failed": [c["name"] for c in results["cases"] if not c["pass"]]}
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(results, indent=2) + "\n")
        print(json.dumps(results["summary"]))
    return 1 if results["summary"]["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
