"""Real ports for the executor: run records, the configuration repository, the cluster, T10.

Every external call is a fixed argument list (no shell). Secret values travel only on stdin to
`kubectl create`; they are never in arguments, files or logs. The repository writer never force
pushes: a moved remote stops the step.
"""

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .applications import ARGO_NAMESPACE
from .stack import StackError

TIMEOUT = 600
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _run(command, *, stdin=None, cwd=None, env=None, check=True, timeout=TIMEOUT):
    result = subprocess.run(command, input=stdin, cwd=cwd, env=env, capture_output=True, text=True,
                            timeout=timeout, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"{Path(command[0]).name} {command[1] if len(command) > 1 else ''} failed")
    return result


class FileRunStore:
    """One JSON file per run, replaced atomically, private to the service user."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, run_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise StackError("invalid_run_id")
        return self.root / f"{run_id}.json"

    def exists(self, run_id):
        return self._path(run_id).exists()

    def load(self, run_id):
        path = self._path(run_id)
        if not path.exists():
            raise StackError("run_unavailable")
        return json.loads(path.read_text())

    def save(self, run):
        fd, temp = tempfile.mkstemp(dir=self.root, prefix=".run-")
        with os.fdopen(fd, "w") as handle:
            json.dump(run, handle, sort_keys=True)
        os.chmod(temp, 0o600)
        os.replace(temp, self._path(run["run_id"]))


class FilePlanStore:
    """Plan documents by id (no secret values: plans hold digests and manifests only)."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def save(self, plan_id, value):
        if not RUN_ID.fullmatch(plan_id):
            raise StackError("invalid_plan_id")
        path = self.root / f"{plan_id}.json"
        if path.exists():
            raise StackError("plan_exists")
        path.write_text(json.dumps(value, sort_keys=True))
        os.chmod(path, 0o600)

    def load(self, plan_id):
        path = self.root / f"{plan_id}.json"
        if not RUN_ID.fullmatch(plan_id) or not path.exists():
            raise StackError("plan_unavailable")
        return json.loads(path.read_text())


class GitConfigRepo:
    """The destination configuration repository (private, one branch). Fast-forward pushes only."""

    AUTHOR = {"GIT_AUTHOR_NAME": "bravo-infra-plan", "GIT_AUTHOR_EMAIL": "infra-plan@bravo.invalid",
              "GIT_COMMITTER_NAME": "bravo-infra-plan", "GIT_COMMITTER_EMAIL": "infra-plan@bravo.invalid"}

    def __init__(self, remote, branch, workdir, *, git="git"):
        self.remote, self.branch, self.workdir, self.git = remote, branch, Path(workdir), git
        if not (self.workdir / ".git").exists():
            self.workdir.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q")
            self._git("remote", "add", "origin", remote)

    def _env(self):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "LANG": "C",
               # No user or system git config: hooks, signing or aliases must not change a commit.
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"} | self.AUTHOR
        if "SSH_AUTH_SOCK" in os.environ:
            env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        return env

    def _git(self, *args, check=True, raw=False):
        out = _run([self.git, *args], cwd=self.workdir, env=self._env(), check=check).stdout
        return out if raw else out.strip()

    @contextlib.contextmanager
    def locked(self):
        """One writer per working directory, across threads, executors and processes."""
        with open(self.workdir / ".git" / "bravo-infra-plan.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def head(self):
        self._git("fetch", "-q", "origin", self.branch)
        return self._git("rev-parse", "FETCH_HEAD")

    def read_tree(self, path, revision=None):
        head = self.head() if revision is None else revision
        if revision is not None and self._git("cat-file", "-t", revision, check=False) != "commit":
            return None
        listing = self._git("ls-tree", "-r", "--name-only", head, *(["--", path] if path else []))
        return {name: self._git("show", f"{head}:{name}", raw=True) for name in listing.splitlines() if name}

    def replace_tree(self, path, files, *, expected_head, message):
        with self.locked():
            return self._replace_tree(path, files, expected_head=expected_head, message=message)

    def _replace_tree(self, path, files, *, expected_head, message):
        if self.head() != expected_head:
            raise StackError("config_repo_moved")
        # Start from exactly the expected commit: no index or work-tree residue of an earlier try.
        self._git("checkout", "-q", "-f", "-B", "work", expected_head)
        self._git("reset", "-q", "--hard", expected_head)
        self._git("clean", "-q", "-fdx")
        target = self.workdir / path
        if target.exists():
            shutil.rmtree(target)  # Git only; prune is off, so no cluster resource is deleted
        for name, text in files.items():
            file = self.workdir / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text)
        self._git("add", "-A", "--", path)
        if self._git("diff", "--cached", "--name-only").splitlines() and any(
                not name.startswith(path + "/") for name in self._git("diff", "--cached", "--name-only").splitlines()):
            raise StackError("config_repo_unexpected_change")
        self._git("commit", "-q", "-m", message, "--", path)
        commit = self._git("rev-parse", "HEAD")
        pushed = _run([self.git, "push", "-q", "origin", f"{commit}:refs/heads/{self.branch}"],
                      cwd=self.workdir, check=False, env=self._env())
        if pushed.returncode != 0:
            text = pushed.stderr
            if "non-fast-forward" in text or "fetch first" in text or "rejected" in text:
                raise StackError("config_repo_moved")  # never force
            raise RuntimeError("git push failed")  # transport or credential: HOLD, not a verdict
        return commit


class KubectlCluster:
    """kubectl and helm against one destination cluster (kubeconfig given by the operator)."""

    FIELD_MANAGER = "bravo-infra-plan"

    def __init__(self, kubeconfig, *, kubectl="kubectl", helm="helm", context=None):
        self.kubeconfig, self.kubectl, self.helm = str(kubeconfig), kubectl, helm
        # Pin the context once: a later `kubectl config use-context` must not move the target.
        self.context = context or _run([kubectl, "config", "current-context"], env=self._env()).stdout.strip()

    def _env(self):
        return {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
                "KUBECONFIG": self.kubeconfig}

    def _k(self, *args, stdin=None, check=True):
        command = [self.kubectl, *(["--context", self.context] if self.context else []), *args]
        return _run(command, stdin=stdin, env=self._env(), check=check)

    def _get(self, kind, namespace, name):
        result = self._k("get", kind, name, *(["-n", namespace] if namespace else []), "-o", "json", check=False)
        if result.returncode != 0:
            if "NotFound" in result.stderr or "not found" in result.stderr:
                return None
            raise RuntimeError("kubectl get failed")
        return json.loads(result.stdout)

    def identity(self, node_name):
        system = self._get("namespace", None, "kube-system")
        node = self._get("node", None, node_name)
        if system is None or node is None:
            raise StackError("target_identity_unavailable")
        return {"cluster_uid": system["metadata"]["uid"], "node_uid": node["metadata"]["uid"]}

    def node_ready(self, node_name):
        node = self._get("node", None, node_name) or {}
        return any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in node.get("status", {}).get("conditions", []))

    def diff(self, items):
        """True when applying would change the cluster (server-side diff)."""
        manifest = json.dumps({"apiVersion": "v1", "kind": "List", "items": items})
        result = self._k("diff", "--server-side", f"--field-manager={self.FIELD_MANAGER}", "--force-conflicts",
                         "-f", "-", stdin=manifest, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError("kubectl diff failed")  # cannot compare: HOLD, never a blind apply
        return result.returncode == 1

    def apply(self, items):
        manifest = json.dumps({"apiVersion": "v1", "kind": "List", "items": items})
        self._k("apply", "--server-side", f"--field-manager={self.FIELD_MANAGER}", "--force-conflicts",
                "-f", "-", stdin=manifest)

    def secret_exists(self, namespace, name):
        return self._get("secret", namespace, name) is not None

    def create_secret(self, namespace, name, secret_type, values):
        manifest = json.dumps({"apiVersion": "v1", "kind": "Secret", "type": secret_type,
                               "metadata": {"name": name, "namespace": namespace,
                                            "labels": {"app.kubernetes.io/managed-by": "bravo-infra-plan"}},
                               "stringData": values})
        self._k("create", "-f", "-", stdin=manifest)  # create, never replace

    def secret_identities(self, namespace, name, key):
        """Identity names and their action targets of a SeaweedFS s3.json; key material is dropped here."""
        import base64

        obj = self._get("secret", namespace, name)
        if obj is None or key not in obj.get("data", {}):
            return None
        document = json.loads(base64.b64decode(obj["data"][key]))
        return {i["name"]: sorted({a.split(":", 1)[-1] for a in i.get("actions", [])})
                for i in document.get("identities", [])}

    def condition(self, kind, namespace, name, condition):
        obj = self._get(kind, namespace, name)
        if obj is None:
            return False
        status = obj.get("status", {})
        if condition == "Complete" and kind == "Job":
            return bool(status.get("succeeded"))
        return any(c.get("type") == condition and c.get("status") == "True" for c in status.get("conditions", []))

    def application_status(self, name):
        obj = self._get("applications.argoproj.io", ARGO_NAMESPACE, name)
        if obj is None:
            return None
        status = obj.get("status", {})
        return {"revision": status.get("sync", {}).get("revision"), "sync": status.get("sync", {}).get("status"),
                "health": status.get("health", {}).get("status")}

    def statefulset_ready(self, namespace, name):
        obj = self._get("statefulset", namespace, name)
        status = (obj or {}).get("status", {})
        return bool(obj) and status.get("readyReplicas", 0) >= 1 and status.get("readyReplicas") == status.get("replicas")

    def deployments_available(self, namespace):
        listing = json.loads(self._k("get", "deployments", "-n", namespace, "-o", "json").stdout)["items"]
        return bool(listing) and all(any(c.get("type") == "Available" and c.get("status") == "True"
                                          for c in d.get("status", {}).get("conditions", [])) for d in listing)

    def labelled_secret_exists(self, namespace, selector):
        listing = json.loads(self._k("get", "secrets", "-n", namespace, "-l", selector, "-o", "json").stdout)
        return bool(listing["items"])


class T10Approvals:
    """T10 `approval_kind=infra` (#603): read the receipt and claim it once per run."""

    def __init__(self, base_url, token, *, context=None, timeout=30):
        self.base_url, self.token, self.context, self.timeout = base_url.rstrip("/"), token, context, timeout

    def _call(self, method, path, body=None):
        request = urllib.request.Request(
            self.base_url + path, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                code = json.loads(error.read())["detail"]["code"]
            except Exception:
                code = f"t10_http_{error.code}"
            raise StackError(code) from None

    def read(self, plan_id, approval_id):
        return self._call("GET", f"/v1/plans/{plan_id}/infra-approvals/{approval_id}")

    def claim(self, plan_id, approval_id, *, run_id, plan_digest):
        return self._call("POST", f"/v1/plans/{plan_id}/infra-approvals/{approval_id}/claims",
                          {"run_id": run_id, "plan_digest": plan_digest})


class AuditLedger:
    """T24 hash chain per infra run (source `infra`): `./store` or `s3://bucket/audit` (Object Lock).

    The T24 chain is per (plan, run): the migration plan the approval belongs to and the infra run.
    A restarted process resumes the stored chain; a replayed identical event is absorbed. Events hold
    step ids, statuses, reasons, digests and times only, and T24 refuses secret-looking payloads.
    """

    def __init__(self, target, store, *, retention_days=1, client=None):
        from bravo_audit.sink import FileSink, ObjectLockSink
        from bravo_audit.storage import split_s3

        self.target, self.store, self.client = target, store, client
        if target.startswith("s3://"):
            import boto3

            self.client = client or boto3.client("s3")
            bucket, prefix = split_s3(target)
            self.sink = ObjectLockSink(self.client, bucket, retention_days=retention_days, prefix=prefix)
        else:
            self.sink = FileSink(Path(target))
        self.ledgers, self.lock = {}, __import__("threading").Lock()

    def __call__(self, event):
        from bravo_audit.ledger import RunLedger
        from bravo_audit.storage import load_plan_runs

        from .stack import canonical, sha256_text

        with self.lock:
            plan_id = self.store.load(event["run_id"])["migration_plan_id"]
            key = (plan_id, event["run_id"])
            if key not in self.ledgers:
                stored = load_plan_runs(self.target, plan_id, client=self.client).get(event["run_id"], [])
                self.ledgers[key] = RunLedger.resume(plan_id, event["run_id"], stored)
            payload = {k: v for k, v in event.items() if v is not None}
            origin = f"t45:{event['run_id']}:{sha256_text(canonical(payload))[:32]}"
            record, created = self.ledgers[key].append("infra", event["event"], payload, origin)
            if created:
                self.sink.put(record)


class AwsIdentity:
    """The AWS account a terraform stack runs against (STS), from the operator's own session."""

    def __init__(self, *, aws="aws"):
        self.aws = aws

    def __call__(self, params):
        env = {k: os.environ[k] for k in ("PATH", "HOME", "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
                                          "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE") if k in os.environ}
        result = _run([self.aws, "sts", "get-caller-identity", "--output", "json"], env=env)
        return {"account_id": json.loads(result.stdout)["Account"]}


class ClusterChecks:
    """Named checks used by `probe` and `operator_action` steps.

    True: observed done. False: observed not done. None: code cannot observe it (a host
    directory, an offline image import); a human confirmation is then required.
    """

    def __init__(self, cluster):
        self.cluster = cluster

    def check(self, name, run):
        params, target = run["plan"]["request"]["params"], run["plan"]["target"]
        c = self.cluster
        checks = {
            "target-identity": lambda: c.identity(params["node_name"]) == target and c.node_ready(params["node_name"]),
            "host-paths": lambda: None,
            "openbao-image": lambda: None,
            "iac-argocd": lambda: c.statefulset_ready(ARGO_NAMESPACE, "bravo-iac-gitops-application-controller"),
            "argocd-repository": lambda: c.labelled_secret_exists(ARGO_NAMESPACE, "argocd.argoproj.io/secret-type=repository"),
            "seaweedfs-auth": lambda: self._seaweedfs_matches_desired(run),
            "tailscale-oauth": lambda: c.secret_exists("tailscale-system", "operator-oauth"),
            "openbao-tls": lambda: c.secret_exists("bravo-secrets", "target-openbao-tls"),
            "openbao-ready": lambda: c.condition("Pod", "bravo-secrets", "target-openbao-0", "Ready"),
            "target-checks": lambda: (c.condition("Cluster", "target-dev", "target-db", "Ready")
                                      and c.deployments_available("target-dev")),
        }
        if name not in checks:
            raise StackError("unknown_check")
        return checks[name]()

    def _seaweedfs_matches_desired(self, run):
        """The Secret exists and its shape is T44's desired state: anonymous read iff open-demo."""
        step = next(s for s in run["stack"]["steps"] if s.get("confirm") == "seaweedfs-auth")
        desired = next(e for e in run["plan"]["steps"] if e["id"] == step["id"]).get("desired", {})
        identities = self.cluster.secret_identities("target-dev", "target-seaweedfs-auth", "s3.json")
        if identities is None:
            return False
        bucket, state = desired.get("bucket"), desired.get("state")
        if not {"bootstrap", "client"} <= set(identities) or identities["client"] != [bucket]:
            return False
        return ("anonymous" in identities) == (state == "open-demo")
