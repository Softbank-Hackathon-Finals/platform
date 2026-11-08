"""Data-security scenarios against a deployed Executor on its real DB pair (T25 live items, T50).

The runner is the T10 client (the command API, the same request contract) and the observer. It never acts as
the Executor: every remote change in a scenario is the Executor's, except the faults it injects on purpose.

WriterWatch is the single-writer check (design 8.3: at any moment at most one DB takes business writes). Four
times a second it opens a fresh connection to each DB as chat_app and inserts one marked row. A write accepted
by the source after the destination accepted one is split brain; so is a destination write before the source's
last. Fresh connections matter: fencing is NOLOGIN plus terminated sessions, and a pooled session would hide it.

Scenarios and what they must show
    rejections   deadline_passed, unknown_environment, connection_ref_mismatch, idempotency_key_conflict and
                 a replayed key returning the same execution; nothing remote changes (WriterWatch: source only)
    cutover      DONE; the source's last write precedes the destination's first
    stale_epoch  a request at an epoch not above the pair's highest is refused (stale_epoch), nothing changes
    lease        a second request while one runs is refused (lease_held)
    failback     a higher epoch reversed: DONE, the order of writers reversed
    restart      the Executor Pod is deleted mid-run; the new process ends DONE or SAFE_HOLD, never two writers
    lingering    a chat_app transaction open on the old writer when the fence comes never commits
    path_cut     the replication path to the destination is cut: ABORT_PRE_WRITE before any write stop
    mismatch     a row only the destination has: the comparison refuses, the destination never opens

usage: python pair_scenarios.py <scenarios.json> <scenario> [<scenario> ...]
Receipts go to cfg["receipts_dir"]/<run>-<scenario>.json (private: they name environments and resources).
"""

import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

TERMINAL = {"DONE", "ABORT_PRE_WRITE", "SAFE_HOLD"}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Cluster:
    def __init__(self, kubeconfig, uid):
        self.env = {**os.environ, "KUBECONFIG": kubeconfig}
        self.uid = uid

    def kubectl(self, *args, input=None, check=True, timeout=120):
        r = subprocess.run(["kubectl", *args], env=self.env, input=input, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"kubectl {args[0]} failed: {r.stderr.strip()[:300]}")
        return r.stdout

    def verify(self):
        if self.kubectl("get", "ns", "kube-system", "-o", "jsonpath={.metadata.uid}").strip() != self.uid:
            raise SystemExit("cluster identity differs from the registered UID; refusing to run")

    def forward(self, namespace, target, port):
        """kubectl port-forward on a free local port; the caller stops it."""
        local = _free_port()
        proc = subprocess.Popen(["kubectl", "port-forward", "-n", namespace, target, f"{local}:{port}"], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", local)) == 0:
                    return proc, local
            time.sleep(0.25)
        proc.kill()
        raise RuntimeError(f"port-forward to {namespace}/{target} did not open")


class Api:
    def __init__(self, port, ca_file, read_token, command_token):
        self.base = f"https://127.0.0.1:{port}"
        self.ctx = ssl.create_default_context(cafile=ca_file)
        self.read, self.command = read_token, command_token

    def call(self, method, path, body=None, key=None):
        token = self.command if method != "GET" else self.read
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        req = urllib.request.Request(self.base + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")


class WriterWatch(threading.Thread):
    def __init__(self, sides, interval=0.25):
        super().__init__(daemon=True)
        self.sides, self.interval, self.events, self.stop = sides, interval, [], threading.Event()

    def run(self):
        seq = 0
        while not self.stop.is_set():
            for name, dsn in self.sides.items():
                seq += 1
                t = time.monotonic()
                try:
                    with psycopg.connect(dsn, connect_timeout=2, autocommit=True) as c:
                        c.execute("INSERT INTO messages (idempotency_key, author, body) VALUES (%s, 'writer-watch', %s)",
                                  (str(uuid.uuid4()), f"{name} {seq}"))
                    self.events.append((t, name, True, ""))
                except psycopg.Error as e:
                    self.events.append((t, name, False, type(e).__name__))
            self.stop.wait(self.interval)

    def verdict(self, first, second):
        """`first` was the writer; after a cutover only `second` may write, never both and never interleaved."""
        ok = {n: [t for t, side, good, _ in self.events if side == n and good] for n in (first, second)}
        last_first = max(ok[first], default=None)
        first_second = min(ok[second], default=None)
        split = last_first is not None and first_second is not None and last_first >= first_second
        return {"probes": len(self.events), "accepted": {n: len(v) for n, v in ok.items()},
                "gap_s": round(first_second - last_first, 3) if not split and None not in (last_first, first_second)
                else None, "split_brain": split}


class Scenarios:
    def __init__(self, cfg):
        self.cfg = cfg
        self.platform = Cluster(cfg["executor"]["kubeconfig"], cfg["executor"]["cluster_uid"])
        self.source = Cluster(cfg["source"]["kubeconfig"], cfg["source"]["cluster_uid"])
        self.target = Cluster(cfg["target"]["kubeconfig"], cfg["target"]["cluster_uid"])
        for c in (self.platform, self.source, self.target):
            c.verify()
        vault = Path(cfg["vault_dir"])
        read = lambda name: json.loads((vault / f"{name}.json").read_text())  # noqa: E731
        name = cfg["executor"]["name"]
        self.tokens, tls = read(f"{name}-tokens"), read(f"{name}-tls")
        self.app_password = read("source-credentials")["chat_app"]
        self.tmp = tempfile.TemporaryDirectory()
        self.ca = self._file("executor-ca.crt", tls["ca.crt"])
        self.procs = []
        proc, port = self.platform.forward("bravo-platform", f"svc/{name}", 8080)
        self.procs.append(proc)
        self.api = Api(port, self.ca, self.tokens["api"], self.tokens["command"])
        self.run_id = datetime.now(UTC).strftime("%m%d%H%M")
        self.epoch = self._highest_epoch()

    def _file(self, name, text):
        path = Path(self.tmp.name) / name
        path.write_text(text)
        return str(path)

    def _highest_epoch(self):
        # The pair's highest epoch is the Executor's; a scenario only ever asks above it.
        return int(self.cfg.get("epoch_floor", 0))

    def close(self):
        for p in self.procs:
            p.terminate()

    # --- helpers -----------------------------------------------------------------------------------------
    def request(self, plan, epoch, *, reverse=False, deadline_s=900, **override):
        s, t = self.cfg["source"], self.cfg["target"]
        a, b = (t, s) if reverse else (s, t)
        body = {"plan_id": plan, "epoch": epoch,
                "source": {"environment_id": a["environment_id"], "connection_ref": a["connection_ref"]},
                "target": {"environment_id": b["environment_id"], "connection_ref": b["connection_ref"]},
                "write_pause_budget_ms": 30000,
                "deadline": (datetime.now(UTC) + timedelta(seconds=deadline_s)).isoformat()}
        body.update(override)
        return body

    def submit(self, body, key=None):
        return self.api.call("POST", "/v1/cutovers", body, key or str(uuid.uuid4()))

    def wait(self, execution_id, timeout_s=600, on_step=None):
        deadline, seen = time.monotonic() + timeout_s, set()
        while time.monotonic() < deadline:
            code, view = self.api.call("GET", f"/v1/cutovers/{execution_id}")
            if code == 200:
                if on_step:
                    for step in (view.get("steps") or {}):
                        if step not in seen:
                            seen.add(step)
                            on_step(step, view)
                if view["status"] in TERMINAL:
                    return view
            time.sleep(1)
        raise TimeoutError(f"execution {execution_id} not terminal in {timeout_s}s")

    def watch(self):
        sides = {}
        for side, cluster in (("source", self.source), ("target", self.target)):
            c = self.cfg[side]
            proc, port = cluster.forward(c["namespace"], c["db_target"], 5432)
            self.procs.append(proc)
            ca = self._file(f"{side}-db-ca.crt", cluster.kubectl("get", "secret", c["db_ca_secret"], "-n", c["namespace"],
                                                                 "-o", "jsonpath={.data.ca\\.crt}"))
            Path(ca).write_bytes(__import__("base64").b64decode(Path(ca).read_text()))
            sides[side] = (f"host=127.0.0.1 port={port} dbname=chat user=chat_app password={self.app_password} "
                           f"sslmode=verify-ca sslrootcert={ca}")
        w = WriterWatch(sides)
        w.start()
        return w

    def psql(self, side, sql):
        c = self.cfg[side]
        cluster = self.source if side == "source" else self.target
        return cluster.kubectl("exec", "-i", "-n", c["namespace"], c["db_target"], "-c", "postgres", "--", "psql",
                               *c.get("psql_args", []), "-v", "ON_ERROR_STOP=1", "-X", "-q", "-At", "-U", "postgres",
                               "-d", "chat", "-f", "-", input=sql).strip()

    def next_epoch(self):
        self.epoch += 1
        return self.epoch

    def plan(self, scenario):
        return f"t50-{self.run_id}-{scenario}"

    # --- scenarios ---------------------------------------------------------------------------------------
    def rejections(self):
        w = self.watch()
        out, epoch = {}, self.epoch + 1000  # far above: only the request checks may refuse these
        out["deadline_passed"] = self.submit(self.request(self.plan("rej"), epoch, deadline_s=-60))
        unknown = self.request(self.plan("rej"), epoch)
        unknown["target"]["environment_id"] = "env-unregistered"
        out["unknown_environment"] = self.submit(unknown)
        wrong_ref = self.request(self.plan("rej"), epoch)
        wrong_ref["target"]["connection_ref"] = "cutover/other"
        out["connection_ref_mismatch"] = self.submit(wrong_ref)
        same = self.request(self.plan("rej"), epoch)
        same["target"] = dict(same["source"])
        out["same_resource"] = self.submit(same)
        time.sleep(3)
        w.stop.set()
        w.join()
        verdict = {k: (code, (body or {}).get("detail", {}).get("code") if isinstance(body, dict) else None)
                   for k, (code, body) in out.items()}
        return {"responses": verdict, "writers": w.verdict("source", "target"),
                "pass": all(code >= 400 for code, _ in verdict.values())
                and w.verdict("source", "target")["accepted"]["target"] == 0}

    def cutover(self, reverse=False, scenario="cutover", inject=None):
        first, second = ("target", "source") if reverse else ("source", "target")
        w = self.watch()
        time.sleep(2)  # the old writer writes before the run begins
        key, epoch = str(uuid.uuid4()), self.next_epoch()
        body = self.request(self.plan(scenario), epoch, reverse=reverse)
        code, view = self.submit(body, key)
        result = {"submitted": [code, view.get("status") if isinstance(view, dict) else view], "epoch": epoch}
        if code in (200, 202):
            replay = self.submit(body, key)
            result["replay_same_execution"] = replay[0] == 200 and replay[1].get("execution_id") == view["execution_id"]
            conflict = self.submit({**body, "write_pause_budget_ms": 29000}, key)
            result["idempotency_conflict"] = conflict[0], (conflict[1] or {}).get("detail", {}).get("code")
            if inject:
                result["fault"] = inject(view["execution_id"])
            final = self.wait(view["execution_id"])
            result["final"] = {"status": final["status"], "result": final.get("result"),
                               "steps": {k: v.get("status") if isinstance(v, dict) else v
                                         for k, v in (final.get("steps") or {}).items()}}
        time.sleep(3)  # the new writer writes after the run ends
        w.stop.set()
        w.join()
        result["writers"] = w.verdict(first, second)
        return result

    def stale_epoch(self):
        code, body = self.submit(self.request(self.plan("stale"), self.epoch))  # not above the highest seen
        return {"response": [code, (body or {}).get("detail", {}).get("code")], "pass": code == 409}

    def lease(self):
        def second(_):
            code, body = self.submit(self.request(self.plan("lease-second"), self.epoch + 1, reverse=True))
            return {"second_request": [code, (body or {}).get("detail", {}).get("code")]}
        return self.cutover(scenario="lease", inject=second)

    def restart(self, reverse=False):
        def kill(execution_id):
            deleted = {}

            def on_step(step, _view):
                if not deleted and step in self.cfg.get("restart_after_steps", ["fence"]):
                    deleted["at_step"] = step
                    deleted["pod"] = self.platform.kubectl("delete", "pod", "-n", "bravo-platform", "-l",
                                                           f"app.kubernetes.io/name={self.cfg['executor']['name']}",
                                                           "--wait=false").strip()[:120]
            try:
                self.wait(execution_id, timeout_s=300, on_step=on_step)
            except (TimeoutError, OSError, urllib.error.URLError):
                pass  # the API goes away with the Pod; the caller waits again on the new one
            self._reforward()
            return deleted
        return self.cutover(reverse=reverse, scenario="restart", inject=kill)

    def _reforward(self):
        name = self.cfg["executor"]["name"]
        self.platform.kubectl("rollout", "status", "-n", "bravo-platform", f"deployment/{name}", "--timeout=300s")
        self.procs[0].terminate()
        proc, port = self.platform.forward("bravo-platform", f"svc/{name}", 8080)
        self.procs[0] = proc
        self.api = Api(port, self.ca, self.tokens["api"], self.tokens["command"])

    def lingering(self, reverse=False):
        old = "target" if reverse else "source"
        holder = {}

        def hold():
            c = self.cfg[old]
            cluster = self.source if old == "source" else self.target
            proc, port = cluster.forward(c["namespace"], c["db_target"], 5432)
            self.procs.append(proc)
            dsn = (f"host=127.0.0.1 port={port} dbname=chat user=chat_app password={self.app_password} "
                   "sslmode=require")
            try:
                with psycopg.connect(dsn) as conn:
                    conn.execute("INSERT INTO messages (idempotency_key, author, body) VALUES (%s, 'lingering', 'open')",
                                 (str(uuid.uuid4()),))
                    time.sleep(self.cfg.get("lingering_s", 60))
                    conn.commit()
                    holder["committed"] = True
            except psycopg.Error as e:
                holder["committed"], holder["error"] = False, type(e).__name__
        t = threading.Thread(target=hold, daemon=True)
        t.start()
        time.sleep(2)
        result = self.cutover(reverse=reverse, scenario="lingering")
        t.join(timeout=120)
        result["lingering_transaction"] = holder
        result["pass"] = holder.get("committed") is False and not result["writers"]["split_brain"]
        return result

    def mismatch(self):
        marker = str(uuid.uuid4())
        self.psql("target", "SET session_replication_role = replica;\n"
                            f"INSERT INTO messages (idempotency_key, author, body) VALUES ('{marker}', 'divergence', 'x');")
        try:
            result = self.cutover(scenario="mismatch")
        finally:
            self.psql("target", "SET session_replication_role = replica;\n"
                                f"DELETE FROM messages WHERE idempotency_key = '{marker}';")
        result["pass"] = result.get("final", {}).get("status") != "DONE" and result["writers"]["accepted"]["target"] == 0
        return result

    def path_cut(self):
        cut = self.cfg["path_cut"]  # {"cluster": "target", "namespace": ..., "kind": "statefulset", "name": ...}
        cluster = self.source if cut["cluster"] == "source" else self.target
        cluster.kubectl("scale", cut["kind"], cut["name"], "-n", cut["namespace"], "--replicas=0")
        try:
            result = self.cutover(scenario="path_cut")
        finally:
            cluster.kubectl("scale", cut["kind"], cut["name"], "-n", cut["namespace"], "--replicas=1")
        result["pass"] = (result.get("final", {}).get("status") == "ABORT_PRE_WRITE"
                          and result["writers"]["accepted"]["target"] == 0)
        return result

    def failback(self):
        return self.cutover(reverse=True, scenario="failback")


def main():
    cfg = json.loads(Path(sys.argv[1]).read_text())
    s = Scenarios(cfg)
    out_dir = Path(cfg["receipts_dir"])
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        for name in sys.argv[2:]:
            started = time.monotonic()
            receipt = getattr(s, name)()
            receipt.update(scenario=name, run=s.run_id, seconds=round(time.monotonic() - started, 1))
            path = out_dir / f"{s.run_id}-{name}.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.write(fd, json.dumps(receipt, indent=1, default=str).encode())
            os.close(fd)
            print(json.dumps({"scenario": name, "status": (receipt.get("final") or {}).get("status"),
                              "pass": receipt.get("pass"), "writers": receipt.get("writers")}))
    finally:
        s.close()


if __name__ == "__main__":
    main()
