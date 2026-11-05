#!/usr/bin/env python3
"""The real CNPG 1.30.1 exporter, log format and Subscription controller, in a throwaway k3s container.

Renders with the T6 and T30 renderers, so the Cluster and Subscription objects are the ones an operator would
apply. Only the storage class of the source differs. A frozen walsender stands in for a broken link, and both
databases share one node. Only containers named t8-health-k3s-* are touched. Prints JSON without credentials.
"""
import argparse
import base64
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKLOADS = HERE.parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(WORKLOADS))
sys.path.insert(0, str(HERE))

import render
import replication
from subscription_metrics import parse_exposition

K3S_IMAGE = "mirror.gcr.io/rancher/k3s:v1.35.5-k3s1"
OPERATOR_MANIFEST = "https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v1.30.1/cnpg-1.30.1.yaml"
OPERATOR_IMAGE = "ghcr.io/cloudnative-pg/cloudnative-pg"
FORWARD = replication.names("target")[2]
CR = "subscriptions.postgresql.cnpg.io/t8-source-to-target"
ROW_VALUE = "row-value-marker"
INCIDENT_TOOL = WORKLOADS / "replication_incident.py"


def load_source_renderer():
    spec = importlib.util.spec_from_file_location("source_render", ROOT / "infra/envs/source-workloads/render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def until(condition, seconds, every=0.5, what="condition"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(every)
    raise TimeoutError(what)


def basic_auth(name, namespace, username, password):
    return {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/basic-auth",
            "metadata": {"name": name, "namespace": namespace}, "stringData": {"username": username, "password": password}}


class K3s:
    def __init__(self, work):
        self.name = f"t8-health-k3s-{uuid.uuid4().hex[:8]}"
        self.kubeconfig = Path(work) / "kubeconfig"

    def __enter__(self):
        docker = ["docker", "run", "-d", "--privileged", "--name", self.name, "-p", "127.0.0.1::6443", "--tmpfs", "/run", "--tmpfs", "/var/run",
                  K3S_IMAGE, "server", "--disable=traefik", "--disable=metrics-server", "--disable=servicelb", "--tls-san=127.0.0.1"]
        subprocess.run(docker, check=True, capture_output=True, text=True)
        try:
            until(lambda: subprocess.run(["docker", "exec", self.name, "test", "-f", "/etc/rancher/k3s/k3s.yaml"], capture_output=True, check=False).returncode == 0,
                  120, 2, "k3s config")
            address = subprocess.run(["docker", "port", self.name, "6443"], check=True, capture_output=True, text=True).stdout.splitlines()[0]
            config = subprocess.run(["docker", "exec", self.name, "cat", "/etc/rancher/k3s/k3s.yaml"], check=True, capture_output=True, text=True).stdout
            self.kubeconfig.write_text(config.replace("https://127.0.0.1:6443", f"https://{address}"))
            until(lambda: self.kubectl("get", "nodes", "--no-headers", check=False).find(" Ready") >= 0, 120, 2, "node ready")
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        subprocess.run(["docker", "rm", "-fv", self.name], capture_output=True, check=False)

    def kubectl(self, *args, input_text=None, check=True):
        done = subprocess.run(["kubectl", f"--kubeconfig={self.kubeconfig}", *args], input=input_text, capture_output=True, text=True, check=False)
        if check and done.returncode:
            raise RuntimeError(f"kubectl {args[0]} failed: {done.stderr.strip()[:400]}")
        return done.stdout

    def apply(self, *objects):
        self.kubectl("apply", "-f", "-", input_text=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(objects)}))

    def host(self, *command):
        subprocess.run(["docker", "exec", self.name, *command], check=True, capture_output=True, text=True)

    def wait_cluster(self, namespace, name):
        def healthy():
            cluster = json.loads(self.kubectl("-n", namespace, "get", "cluster", name, "-o", "json", check=False) or "{}")
            return cluster.get("status", {}).get("phase") == "Cluster in healthy state"

        until(healthy, 600, 5, f"{name} healthy")


class Db:
    def __init__(self, k3s, namespace, pod):
        self.k3s, self.namespace, self.pod = k3s, namespace, pod

    def rows(self, sql):
        output = self.k3s.kubectl("-n", self.namespace, "exec", self.pod, "-c", "postgres", "--", "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-d", "chat", "-c", sql)
        return [line.split("|") for line in output.splitlines()]

    def one(self, sql):
        return self.rows(sql)[0]

    def signal(self, name, pid):
        self.k3s.kubectl("-n", self.namespace, "exec", self.pod, "-c", "postgres", "--", "sh", "-c", f"kill -{name} {int(pid)}")

    def logs(self, since):
        return self.k3s.kubectl("-n", self.namespace, "logs", self.pod, "-c", "postgres", f"--since-time={since}")

    def metrics(self):
        return parse_exposition(self.k3s.kubectl("get", "--raw", f"/api/v1/namespaces/{self.namespace}/pods/{self.pod}:9187/proxy/metrics"))


def subscription_state(target):
    enabled, on_error, workers, errors, pid = target.one(f"""
        SELECT s.subenabled, s.subdisableonerr,
               (SELECT count(*) FROM pg_stat_subscription w WHERE w.subid = s.oid AND w.worker_type = 'apply'),
               e.apply_error_count, coalesce((SELECT w.pid FROM pg_stat_subscription w WHERE w.subid = s.oid AND w.worker_type = 'apply'), 0)
        FROM pg_subscription s JOIN pg_stat_subscription_stats e ON e.subid = s.oid WHERE s.subname = '{FORWARD}'""")
    return {"enabled": enabled == "t", "disable_on_error": on_error == "t", "workers": int(workers), "errors": int(errors), "worker_pid": int(pid)}


def exported(target):
    return target.metrics()["samples"].get(FORWARD)


def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def incident_report(log_text, environ=None):
    """The documented command, as its own process, so what is measured is what an operator runs."""
    command = [str(INCIDENT_TOOL)] if environ else [sys.executable, str(INCIDENT_TOOL)]
    done = subprocess.run(command, input=log_text, capture_output=True, text=True, check=False, env={**os.environ, **(environ or {})})
    return {"exit": done.returncode, "report": json.loads(done.stdout) if done.stdout.strip() else None, "stderr_bytes": len(done.stderr), "stdout": done.stdout}


def prepare(k3s):
    """Both databases and the forward subscription, without the new monitoring block."""
    manifest = urllib.request.urlopen(OPERATOR_MANIFEST, timeout=60).read().decode()
    digest = re.search(r"tag: (\S+)", (WORKLOADS / "cnpg-values.yaml").read_text()).group(1)
    manifest = manifest.replace(f"{OPERATOR_IMAGE}:1.30.1", f"{OPERATOR_IMAGE}:{digest}")
    k3s.kubectl("apply", "--server-side", "-f", "-", input_text=manifest)
    k3s.kubectl("-n", "cnpg-system", "rollout", "status", "deployment/cnpg-controller-manager", "--timeout=300s")
    passwords = {"source_replicator": secrets.token_hex(16), "target_replicator": secrets.token_hex(16)}

    source_render = load_source_renderer()
    namespace, _, schema, source_cluster = source_render.database()
    source_cluster["spec"]["storage"]["storageClass"] = "local-path"
    k3s.apply(namespace)
    k3s.apply(basic_auth("source-app-db", "source-dev", "chat_app", secrets.token_hex(16)),
              basic_auth("source-replication", "source-dev", "chat_replicator", passwords["source_replicator"]), schema, source_cluster)

    node = k3s.kubectl("get", "nodes", "-o", "jsonpath={.items[0].metadata.name}")
    k3s.host("mkdir", "-p", "/var/lib/bravo-target-dev/postgres")
    k3s.host("chown", "26:26", "/var/lib/bravo-target-dev/postgres")
    objects = render.database(node)
    target_cluster = next(item for item in objects if item["kind"] == "Cluster")
    monitoring = target_cluster["spec"].pop("monitoring")
    first = [item for item in objects if item["kind"] != "ConfigMap"]
    k3s.apply(first[0])
    k3s.apply(basic_auth("target-app-db", "target-dev", "chat_app", secrets.token_hex(16)),
              basic_auth("target-replication", "target-dev", "chat_replicator", passwords["target_replicator"]), *first[1:])
    k3s.wait_cluster("source-dev", "source-db")
    k3s.wait_cluster("target-dev", "target-db")
    k3s.apply(*render.migration())
    k3s.kubectl("-n", "target-dev", "wait", "--for=condition=complete", "job/target-chat-schema-v1", "--timeout=180s")

    ca = k3s.kubectl("-n", "source-dev", "get", "secret", "source-db-ca", "-o", "jsonpath={.data.ca\\.crt}")
    k3s.apply({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "t8-source-replication", "namespace": "target-dev"},
               "stringData": {"password": passwords["source_replicator"]}},
              {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "t8-source-ca", "namespace": "target-dev"}, "data": {"ca.crt": ca}})
    address = k3s.kubectl("-n", "source-dev", "get", "svc", "source-db-rw", "-o", "jsonpath={.spec.clusterIP}")
    patch = replication.external_patch("target", address, json.loads(k3s.kubectl("-n", "target-dev", "get", "cluster", "target-db", "-o", "json")))
    k3s.kubectl("-n", "target-dev", "patch", "cluster", "target-db", "--type", "json", "-p", json.dumps(patch))
    k3s.wait_cluster("target-dev", "target-db")
    k3s.apply(replication.subscription("target"))
    target = Db(k3s, "target-dev", "target-db-1")
    # The operator creates it a moment later, so an empty catalog means not yet.
    until(lambda: target.rows(f"SELECT 1 FROM pg_stat_subscription WHERE subname = '{FORWARD}' AND worker_type = 'apply' AND pid IS NOT NULL"),
          180, 2, "apply worker")
    return passwords, monitoring


def pod_identity(k3s):
    pod = json.loads(k3s.kubectl("-n", "target-dev", "get", "pod", "target-db-1", "-o", "json"))
    return {"uid": pod["metadata"]["uid"], "restarts": sum(item["restartCount"] for item in pod["status"]["containerStatuses"])}


def write_row(source):
    return int(source.one("INSERT INTO messages (idempotency_key, author, body) VALUES (gen_random_uuid(), 'health', 'x') RETURNING id")[0])


def wait_row(target, row_id, body="x", seconds=60):
    """That exact row, with the source's body: a conflict leaves a row with the same id and a different body."""
    began = time.monotonic()
    until(lambda: target.one(f"SELECT count(*) FROM messages WHERE id = {int(row_id)} AND body = '{body}'")[0] == "1", seconds, 0.2, "row arrival")
    return round(time.monotonic() - began, 2)


def freeze_walsender(source, seconds):
    """Neither side is told, which is what the receiver timeout exists for."""
    pid = source.one(f"SELECT active_pid FROM pg_replication_slots WHERE slot_name = '{FORWARD}'")[0]
    source.signal("STOP", pid)
    try:
        time.sleep(seconds)
    finally:
        source.signal("CONT", pid)


def exported_until(target, condition, seconds=90):
    """How long the page takes to show a state SQL already shows. The exporter caches query results."""
    def shown():
        page = exported(target)
        return page if page and condition(page) else None

    began = time.monotonic()
    value = until(shown, seconds, 0.5, "exported value")
    return {"after_s": round(time.monotonic() - began, 1), **value}


def inject_conflict(source, target):
    """The target already holds the row the source is about to send, as a bypassing write would leave it."""
    # From the sequence, so that a later ordinary insert can never run into this id.
    row_id = int(source.one("SELECT nextval(pg_get_serial_sequence('public.messages', 'id'))")[0])
    target.rows(f"INSERT INTO messages (id, idempotency_key, author, body) VALUES ({row_id}, gen_random_uuid(), 'conflict', '{ROW_VALUE}')")
    source.rows(f"INSERT INTO messages (id, idempotency_key, author, body) VALUES ({row_id}, gen_random_uuid(), 'conflict', 'x')")
    return row_id


def remove_conflict(target, row_id):
    target.rows(f"DELETE FROM messages WHERE id = {row_id} AND body = '{ROW_VALUE}'")


def run(k3s, work, results):
    passwords, monitoring = prepare(k3s)
    source, target = Db(k3s, "source-dev", "source-db-1"), Db(k3s, "target-dev", "target-db-1")
    results["settings"] = {"wal_receiver_timeout": target.one("SHOW wal_receiver_timeout")[0], "wal_sender_timeout": source.one("SHOW wal_sender_timeout")[0],
                           "postgres": target.one("SHOW server_version")[0]}
    results["subscription_owner"] = target.one(f"SELECT subowner::regrole FROM pg_subscription WHERE subname = '{FORWARD}'")[0]

    # 1. Rolling the metrics out on a database that is already replicating.
    before = {**pod_identity(k3s), "worker_pid": subscription_state(target)["worker_pid"]}
    results["before_monitoring"] = {"metrics_present": bool(target.metrics()["samples"])}
    began = time.monotonic()
    k3s.apply(render.subscription_metrics())
    k3s.kubectl("-n", "target-dev", "patch", "cluster", "target-db", "--type", "merge", "-p", json.dumps({"spec": {"monitoring": monitoring}}))
    until(lambda: exported(target), 180, 1, "metric appears")
    after = {**pod_identity(k3s), "worker_pid": subscription_state(target)["worker_pid"]}
    results["live_rollout"] = {"metric_appeared_s": round(time.monotonic() - began, 1), "pod_replaced": before["uid"] != after["uid"],
                               "restarts_added": after["restarts"] - before["restarts"], "worker_replaced": before["worker_pid"] != after["worker_pid"]}

    # 2. What the real exporter prints, against what SQL says.
    page = target.metrics()
    results["exporter"] = {"types": page["types"], "first_sample": page["samples"][FORWARD], "sql": subscription_state(target),
                           "exporter_role": target.one("SELECT rolname, pg_has_role(rolname, 'pg_monitor', 'member'), rolsuper FROM pg_roles WHERE rolname = 'cnpg_metrics_exporter'")}

    # 3. Default setting: a stalled stream stops the subscription.
    since = now_utc()
    errors = subscription_state(target)["errors"]
    freeze_walsender(source, 9)
    state = subscription_state(target)
    results["stall_with_default"] = {"sql": state, "errors_added": state["errors"] - errors,
                                     "exported": exported_until(target, lambda page: page["enabled"] == 0 and page["apply_workers"] == 0)}
    log = target.logs(since)
    report = incident_report(log)
    results["stall_with_default"]["incident"] = {"exit": report["exit"], "disables": report["report"]["disables"], "counts": report["report"]["counts"]}
    results["log_record_fields"] = sorted(next(json.loads(line)["record"] for line in log.splitlines()
                                               if line.startswith("{") and json.loads(line).get("logger") == "postgres"))

    # 4. The one-line report with both connections, over verify-full, while the subscription is off.
    write_row(source)
    environ = connections(k3s, work, passwords)
    try:
        report = incident_report(target.logs(since), environ["env"])
    finally:
        for forward in environ["forwards"]:
            forward.terminate()
    results["incident_with_connections"] = {"exit": report["exit"], "state": report["report"]["state"], "latest_cause": report["report"]["latest_cause"],
                                            "credential_in_output": any(value in report["stdout"] for value in passwords.values())}

    target.rows(replication.runtime_state_sql("target", True))
    results["resumed"] = {"row_arrived_s": wait_row(target, write_row(source)),
                          "exported": exported_until(target, lambda page: page["enabled"] == 1 and page["apply_workers"] == 1)}

    # 5. Default setting: a conflict stops the subscription, and the log must say so without the value.
    since = now_utc()
    errors = subscription_state(target)["errors"]
    row_id = inject_conflict(source, target)
    until(lambda: not subscription_state(target)["enabled"], 30, 0.5, "disabled by conflict")
    report = incident_report(target.logs(since))
    errors_now = subscription_state(target)["errors"]
    results["conflict_with_default"] = {"errors_added": errors_now - errors,
                                        "exported": exported_until(target, lambda page: page["enabled"] == 0 and page["apply_error_count"] == errors_now),
                                        "disables": report["report"]["disables"], "row_value_in_output": ROW_VALUE in report["stdout"],
                                        "row_value_in_raw_log": ROW_VALUE in target.logs(since)}
    remove_conflict(target, row_id)
    target.rows(replication.runtime_state_sql("target", True))
    wait_row(target, write_row(source))

    # 6. Turning it off through the Subscription object, the way the operator owns this value.
    state = subscription_state(target)
    generation = json.loads(k3s.kubectl("-n", "target-dev", "get", CR, "-o", "json"))["metadata"]["generation"]
    began = time.monotonic()
    k3s.kubectl("-n", "target-dev", "patch", CR, "--type", "merge", "-p", json.dumps({"spec": {"parameters": {"disable_on_error": "false"}}}))
    until(lambda: not subscription_state(target)["disable_on_error"], 90, 0.5, "CR applied")
    took = round(time.monotonic() - began, 1)
    subscription = json.loads(k3s.kubectl("-n", "target-dev", "get", CR, "-o", "json"))
    results["cr_patch_while_enabled"] = {"applied_s": took, "generation": [generation, subscription["metadata"]["generation"]],
                                         "applied": subscription["status"].get("applied"), "worker_replaced": subscription_state(target)["worker_pid"] != state["worker_pid"]}

    # 7. Setting off: the same stall leaves it on and it recovers alone.
    since = now_utc()
    errors = subscription_state(target)["errors"]
    freeze_walsender(source, 9)
    row_id = write_row(source)
    arrived = wait_row(target, row_id)
    errors_now = subscription_state(target)["errors"]
    report = incident_report(target.logs(since))["report"]
    results["stall_with_off"] = {"row_arrived_s": arrived, "errors_added": errors_now - errors, "sql": subscription_state(target),
                                 "exported": exported_until(target, lambda page: page["apply_error_count"] == errors_now),
                                 "incident": {key: report[key] for key in ("disables", "latest_error", "counts")}}

    # 8. Setting off: a conflict retries for ever. Only the error count and the log show it.
    since = now_utc()
    errors = subscription_state(target)["errors"]
    row_id = inject_conflict(source, target)
    samples = []
    began = time.monotonic()
    while time.monotonic() - began < 30:
        samples.append(exported(target))
        time.sleep(1)
    report = incident_report(target.logs(since))
    results["conflict_with_off"] = {"seconds": 30, "errors_added": subscription_state(target)["errors"] - errors,
                                    "exported_enabled_lowest": min(item["enabled"] for item in samples),
                                    "exported_errors_added": samples[-1]["apply_error_count"] - errors,
                                    "disables": report["report"]["disables"], "latest_error": report["report"]["latest_error"],
                                    "row_value_in_output": ROW_VALUE in report["stdout"]}
    remove_conflict(target, row_id)
    results["conflict_with_off"]["healed_after_removing_the_row_s"] = wait_row(target, row_id)

    # 9. A direct ALTER while the object says otherwise.
    target.rows(f'ALTER SUBSCRIPTION "{FORWARD}" SET (disable_on_error = true)')
    time.sleep(70)
    held = subscription_state(target)["disable_on_error"]
    k3s.kubectl("-n", "target-dev", "patch", CR, "--type", "merge", "-p", json.dumps({"spec": {"parameters": {"synchronous_commit": "off"}}}))
    until(lambda: not subscription_state(target)["disable_on_error"], 90, 0.5, "object wins")
    results["direct_alter_against_object"] = {"direct_value_after_70_s": held, "after_next_object_change": subscription_state(target)["disable_on_error"]}
    k3s.kubectl("-n", "target-dev", "patch", CR, "--type", "json", "-p", json.dumps([{"op": "remove", "path": "/spec/parameters/synchronous_commit"}]))
    time.sleep(5)

    # 10. Patching the object while the subscription is off. The reverse direction is always in this state.
    target.rows(replication.runtime_state_sql("target", False))
    until(lambda: subscription_state(target)["workers"] == 0, 30, 0.5, "worker stopped")
    k3s.kubectl("-n", "target-dev", "patch", CR, "--type", "merge", "-p", json.dumps({"spec": {"parameters": {"disable_on_error": "true"}}}))
    def failed():
        status = json.loads(k3s.kubectl("-n", "target-dev", "get", CR, "-o", "json"))["status"]
        return status if status.get("applied") is False else None
    status = until(failed, 60, 1, "object marked failed")
    results["cr_patch_while_disabled"] = {"applied": status["applied"], "message": status.get("message", "")[:200],
                                          "value_changed": subscription_state(target)["disable_on_error"]}
    target.rows(replication.runtime_state_sql("target", True))
    began = time.monotonic()
    until(lambda: subscription_state(target)["disable_on_error"], 120, 1, "applied after enable")
    results["cr_patch_while_disabled"]["applied_after_enable_s"] = round(time.monotonic() - began, 1)
    results["final"] = {"sql": subscription_state(target), "exported": exported(target),
                        "object_applied": json.loads(k3s.kubectl("-n", "target-dev", "get", CR, "-o", "json"))["status"].get("applied")}


def connections(k3s, work, passwords):
    """DSNs through port-forward, keeping verify-full with the certificate names of the Services."""
    env, forwards = {}, []
    for side, password, variable in (("source", passwords["source_replicator"], "T8_RESUME_SOURCE_DSN"),
                                     ("target", passwords["target_replicator"], "T8_RESUME_TARGET_DSN")):
        namespace, service = f"{side}-dev", f"{side}-db-rw"
        ca = base64.b64decode(k3s.kubectl("-n", namespace, "get", "secret", f"{side}-db-ca", "-o", "jsonpath={.data.ca\\.crt}"))
        ca_path = Path(work) / f"{side}-ca.crt"
        ca_path.write_bytes(ca)
        forward = subprocess.Popen(["kubectl", f"--kubeconfig={k3s.kubeconfig}", "-n", namespace, "port-forward", f"svc/{service}", ":5432"],
                                   stdout=subprocess.PIPE, text=True)
        forwards.append(forward)
        port = re.search(r"127\.0\.0\.1:(\d+)", forward.stdout.readline()).group(1)
        env[variable] = (f"host={service}.{namespace}.svc hostaddr=127.0.0.1 port={port} dbname=chat user=chat_replicator "
                         f"password={password} sslmode=verify-full sslrootcert={ca_path}")
    return {"env": env, "forwards": forwards}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    results = {"stand_ins": ["one k3s node holds both clusters", "frozen walsender instead of a broken link", "source storage class is local-path"],
               "k3s_image": K3S_IMAGE, "operator_manifest": OPERATOR_MANIFEST}
    with tempfile.TemporaryDirectory(prefix="t8-health-") as work, K3s(work) as k3s:
        try:
            run(k3s, work, results)
        finally:
            Path(args.out).write_text(json.dumps(results, indent=2) + "\n")
            print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
