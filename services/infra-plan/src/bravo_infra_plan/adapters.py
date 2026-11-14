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
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import replication
from .applications import ARGO_NAMESPACE
from .executor import HOLD, StepOutcome
from .stack import StackError, canonical, environment_of, sha256_text, substitute

# Set on the node by whatever prepared the host directories (first boot or the platform's site agent),
# after it created them exactly as the step declares. The value binds the attestation to that declaration.
HOST_PATHS_ANNOTATION = "bravo.io/host-paths"
TAILSCALE_LOGGED_IN = {"_current-profile"}  # tailscale ipn/store/kubestore key after login


def host_paths_digest(paths):
    return sha256_text(canonical(paths))


def expected_images(expect, params):
    """The image names an `expect` names for this request (`images`, or `images_by_arch[arch]`)."""
    if "images" in expect:
        return expect["images"]
    names = expect["images_by_arch"].get(params.get("arch"))
    if not names:
        raise StackError("image_unavailable_for_arch")
    return names

LOGIN_REALM = "bravo"  # target-workloads/identity.py REALM
# Paths the Edge must not serve on the login host (edge-cloudflare: only /realms/bravo/ and /resources/).
LOGIN_CLOSED_PATHS = ("/admin/master/console/", "/realms/master/.well-known/openid-configuration")
# T44 intake states in which the Application exists and names the approved target (autoSync may be off).
IAC_APPLICATION_ON = frozenset({"SYNC_PAUSED", "SYNC_PENDING", "SYNC_IN_PROGRESS", "SYNC_RETRYING", "SYNC_VERIFIED"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect is an answer to check, not a place to go


def public_get(url, *, timeout=10, limit=65536):
    """GET a public https URL with verified TLS and no redirects: (status, body) or (None, None) if unreachable."""
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as response:
            return response.status, response.read(limit)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, OSError):
        return None, None


def login_public(params, http):
    """The login host serves the bravo realm with this issuer and nothing of the admin side.

    Not reachable yet: False (wait). Open admin paths or another issuer: HOLD, the Edge must be closed."""
    base = params["login_public_url"]
    status, body = http(f"{base}/realms/{LOGIN_REALM}/.well-known/openid-configuration")
    if status != 200:
        return False
    try:
        issuer = json.loads(body).get("issuer")
    except (ValueError, AttributeError):
        return False
    if issuer != f"{base}/realms/{LOGIN_REALM}":
        raise StepOutcome(HOLD, "login_issuer_mismatch")
    for path in LOGIN_CLOSED_PATHS:
        status, _ = http(base + path)
        if status is None:
            return False
        if status != 404:
            raise StepOutcome(HOLD, "login_admin_path_open")
    return True


TIMEOUT = 600
SQL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
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

    # The API was not reached at all (a dropped tunnel or forwarded connection), so nothing was decided.
    TRANSPORT = ("Unable to connect to the server", "broken pipe", "connection reset by peer", "unexpected EOF",
                 "i/o timeout", "TLS handshake timeout", "connection refused")

    def _read(self, *args, stdin=None, attempts=3):
        """A read-only kubectl call, retried a few times only when the request never reached the API."""
        for attempt in range(attempts):
            result = self._k(*args, stdin=stdin, check=False)
            if result.returncode == 0 or not any(t in result.stderr for t in self.TRANSPORT):
                return result
            if attempt + 1 < attempts:
                time.sleep(1 + attempt)
        return result

    def _get(self, kind, namespace, name):
        result = self._read("get", kind, name, *(["-n", namespace] if namespace else []), "-o", "json")
        if result.returncode != 0:
            if "NotFound" in result.stderr or "not found" in result.stderr:
                return None
            first = (result.stderr.strip().splitlines() or [""])[0][:120]  # a read: no value in it
            raise RuntimeError(f"kubectl get {kind} failed: {first}")
        return json.loads(result.stdout)

    def server(self):
        """The API server URL of the pinned context (no credential is read)."""
        return self._k("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}").stdout.strip()

    def cluster_uid(self):
        system = self._get("namespace", None, "kube-system")
        if system is None:
            raise StackError("target_identity_unavailable")
        return system["metadata"]["uid"]

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

    def node_annotation(self, node_name, key):
        node = self._get("node", None, node_name) or {}
        return node.get("metadata", {}).get("annotations", {}).get(key)

    def node_image_names(self, node_name):
        """Every name the kubelet reports for the node's images (tags and repo@sha256 aliases)."""
        node = self._get("node", None, node_name) or {}
        return {name for image in node.get("status", {}).get("images", []) for name in image.get("names", [])}

    def diff(self, items):
        """True when applying would change the cluster (server-side diff)."""
        manifest = json.dumps({"apiVersion": "v1", "kind": "List", "items": items})
        result = self._read("diff", "--server-side", f"--field-manager={self.FIELD_MANAGER}", "--force-conflicts",
                            "-f", "-", stdin=manifest)
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
        result = self._read("get", "deployments", "-n", namespace, "-o", "json")
        if result.returncode != 0:
            raise RuntimeError("kubectl get deployments failed: " + (result.stderr.strip().splitlines() or [""])[0][:120])
        listing = json.loads(result.stdout)["items"]
        return bool(listing) and all(any(c.get("type") == "Available" and c.get("status") == "True"
                                          for c in d.get("status", {}).get("conditions", [])) for d in listing)

    def labelled_secret_exists(self, namespace, selector):
        result = self._read("get", "secrets", "-n", namespace, "-l", selector, "-o", "json")
        if result.returncode != 0:
            raise RuntimeError("kubectl get secrets failed: " + (result.stderr.strip().splitlines() or [""])[0][:120])
        listing = json.loads(result.stdout)
        return bool(listing["items"])

    # --- destination as a migration target (target-onprem-migration) ----------------------------

    def get_object(self, item):
        api = item["apiVersion"]
        resource = item["kind"].lower() + ("." + api.split("/")[0] if "/" in api else "")
        return self._get(resource, item["metadata"].get("namespace"), item["metadata"]["name"])

    def create(self, items):
        """kubectl create: fails on an existing object instead of replacing it."""
        self._k("create", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))

    def secret_keys(self, namespace, name):
        """The data keys of a Secret (never its values); None when it does not exist."""
        obj = self._get("secret", namespace, name)
        return None if obj is None else sorted((obj.get("data") or {}).keys())

    def audit_level_events(self, level):
        """apiserver_audit_level_total for one level since the API server started; None when absent."""
        if level not in ("None", "Metadata", "Request", "RequestResponse"):
            raise StackError("unknown_audit_level")
        for line in self._k("get", "--raw", "/metrics").stdout.splitlines():
            if line.startswith("apiserver_audit_level_total{") and f'level="{level}"' in line:
                return float(line.rsplit(" ", 1)[1])
        return None

    def primary_query(self, namespace, cluster, database, query):
        """One fixed query in a read-only transaction on the primary. None: no single primary right now
        (a switchover). A failed read raises, so the step holds instead of passing."""
        if not SQL_NAME.fullmatch(database):
            raise StackError("invalid_identifier")
        pods = json.loads(self._k("get", "pods", "-n", namespace, "-l",
                                  f"cnpg.io/cluster={cluster},cnpg.io/instanceRole=primary", "-o", "json").stdout)["items"]
        if len(pods) != 1:
            return None
        result = self._k("exec", "-n", namespace, pods[0]["metadata"]["name"], "-c", "postgres", "--",
                         "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", database,
                         "-c", "BEGIN READ ONLY", "-c", query, "-c", "ROLLBACK", check=False)
        if result.returncode != 0:
            raise RuntimeError("primary_read_failed")
        return [line.split("|") for line in result.stdout.strip().splitlines() if line]

    def argo_intake(self, repo_root, environment, cluster_uid, revision):
        """T44's read-only GitOps intake (infra/iac/customer_status.py, #635) on this cluster: the same judge,
        not a copy. It checks the cluster UID, the Application target, the operation and every resource."""
        result = _run([sys.executable, str(Path(repo_root) / "infra/iac/customer_status.py"),
                       "--kubeconfig", self.kubeconfig, *(["--context", self.context] if self.context else []),
                       "--expected-cluster-uid", cluster_uid, "--environment", environment, "--revision", revision],
                      env=self._env(), check=False)
        if result.returncode not in (0, 3):
            raise RuntimeError("argo_intake_failed")
        return json.loads(result.stdout)


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

    def claim(self, plan_id, approval_id, *, run_id, binding):
        """`binding` is the whole expected `{infra_plan_id, plan_digest, requested_by}`; T10 compares all."""
        return self._call("POST", f"/v1/plans/{plan_id}/infra-approvals/{approval_id}/claims",
                          {"run_id": run_id, "binding": binding})


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


def _env(*names):
    return {k: os.environ[k] for k in ("PATH", "HOME", *names) if k in os.environ}


class AwsIdentity:
    """aws: the account a stack runs against (STS), from the operator's own session."""

    def __init__(self, *, aws="aws"):
        self.aws = aws

    def __call__(self, params):
        env = _env("AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE")
        result = _run([self.aws, "sts", "get-caller-identity", "--output", "json"], env=env)
        return {"account_id": json.loads(result.stdout)["Account"]}


class GcpIdentity:
    """gcp: the project of the operator's gcloud configuration, and an active credential.

    The active account must exist but is not part of the binding: another approved operator may resume
    a run, and a personal e-mail never enters a plan document."""

    def __init__(self, *, gcloud="gcloud"):
        self.gcloud = gcloud

    def __call__(self, params):
        env = _env("CLOUDSDK_CONFIG", "CLOUDSDK_ACTIVE_CONFIG_NAME", "CLOUDSDK_CORE_PROJECT")
        project = _run([self.gcloud, "config", "get-value", "project"], env=env).stdout.strip()
        accounts = _run([self.gcloud, "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
                        env=env).stdout.split()
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project) or not accounts:
            raise StackError("gcp_identity_unavailable")
        return {"project_id": project}


class ClusterIdentity:
    """onprem: the kubeconfig cluster's UID (kube-system), and the node's UID when the stack names a node."""

    def __init__(self, cluster):
        self.cluster = cluster

    def __call__(self, params):
        if "node_name" in params:
            return self.cluster.identity(params["node_name"])
        return {"cluster_uid": self.cluster.cluster_uid()}


# both.identity (ADR-0034): a stack is bound to what its kind can name before anything is built.
IDENTITY_KINDS = {"aws": lambda cluster: AwsIdentity(), "gcp": lambda cluster: GcpIdentity(),
                  "onprem": ClusterIdentity}


def identity_for(kind, cluster=None):
    if kind not in IDENTITY_KINDS:
        raise StackError("unsupported_environment_kind")
    if kind == "onprem" and cluster is None:
        raise StackError("cluster_arguments_required")
    return IDENTITY_KINDS[kind](cluster)


class LazyCluster:
    """A cluster that a cloud stack builds during the run: kubectl is pinned on first use, not at start."""

    def __init__(self, factory):
        self._factory, self._cluster, self._lock = factory, None, __import__("threading").Lock()

    def __getattr__(self, name):
        with self._lock:
            if self._cluster is None:
                self._cluster = self._factory()
        return getattr(self._cluster, name)


class CachedOutputs:
    """Terraform outputs per (step, request), read from state once per process; never in a run record.

    `reader_for(params)` returns terraform.outputs_reader for those parameters."""

    def __init__(self, reader_for):
        self.reader_for, self.values, self._lock = reader_for, {}, __import__("threading").Lock()

    def __call__(self, step_id, params):
        key = (step_id, json.dumps(params, sort_keys=True))
        with self._lock:
            if key in self.values:
                return self.values[key]
            value = self.reader_for(params)(step_id)
            if value:  # an unapplied step has no outputs yet: read it again next time
                self.values[key] = value
            return value


def _host(url):
    host = re.sub(r"^[a-z]+://", "", str(url or "")).strip().rstrip("/")
    return host[:-4] if host.endswith(":443") else host
def _iac_application_on(cluster, repo_root, run):
    """T44's intake on this cluster: the Application names the planned environment and iac_revision.

    Still bootstrap only: False. Refused (another target, prune) or a failed sync: HOLD with its reason."""
    if repo_root is None:
        raise StackError("repo_root_required")
    request = run["plan"]["request"]
    result = cluster.argo_intake(repo_root, request["environment_id"], run["plan"]["target"]["cluster_uid"],
                                 request["params"]["iac_revision"])
    if result["status"] in ("BLOCKED", "SYNC_FAILED"):
        raise StepOutcome(HOLD, "argo_" + (result.get("reasons") or [result["status"]])[0].lower())
    return result["status"] in IAC_APPLICATION_ON


def _argo_synced(cluster, repo_root, run):
    """T44's intake says SYNC_VERIFIED at the approved revision. Not installed or not converged yet: wait.
    Something it refuses (another target, prune, a changed read): HOLD with its reason."""
    result = cluster.argo_intake(repo_root, "target-dev", run["plan"]["target"]["cluster_uid"],
                                 run["plan"]["request"]["params"]["iac_revision"])
    if result["status"] == "BLOCKED":
        raise StepOutcome(HOLD, "argo_" + (result.get("reasons") or ["blocked"])[0].lower())
    return result["status"] == "SYNC_VERIFIED"


# The checks each environment kind can observe (A10, ADR-0034). `onprem` is the T45 set for the RKE2
# destination (node, host paths, OpenBao, SeaweedFS, the migration target), read with its fixed names.
# A cloud kind gets only the checks it observes the same way, with the names of the stack's `environment`
# block; any other known name is refused there (`check_unsupported_for_kind`), never passed.
ONPREM_CHECKS = frozenset({
    "target-identity", "host-paths", "openbao-image", "app-image", "iac-argocd", "argocd-repository",
    "seaweedfs-auth", "app-metrics-client", "tailscale-oauth", "tailscale-joined", "openbao-tls", "openbao-ready",
    "target-checks", "login-public", "iac-application-created", "iac-application-enabled",
    "exit-drill-control-ready", "audit-metadata-active", "iac-network-synced", "replication-credentials",
    "replication-external-cluster", "replication-target-ready", "initial-copy-complete",
})
CLOUD_CHECKS = frozenset({
    "cluster-access", "edge-token", "secret-store-ready", "iac-argocd", "argocd-repository", "app-metrics-client",
    "tailscale-oauth", "tailscale-joined", "target-checks", "login-public", "iac-application-created",
})
CHECKS_BY_KIND = {"onprem": ONPREM_CHECKS, "aws": CLOUD_CHECKS, "gcp": CLOUD_CHECKS}
KNOWN_CHECKS = ONPREM_CHECKS | CLOUD_CHECKS


class ClusterChecks:
    """Named checks used by `probe` and `operator_action` steps.

    True: observed done. False: observed not done. None: code cannot observe it (a host
    directory, an offline image import); a human confirmation is then required.
    The stack's environment kind picks the table (`CHECKS_BY_KIND`).
    """

    def __init__(self, cluster, repo_root=None, *, http=public_get, outputs=None):
        self.cluster, self.repo_root, self.http, self.outputs = cluster, repo_root, http, outputs

    def check(self, name, run):
        kind = environment_of(run["stack"])["kind"]
        if kind not in CHECKS_BY_KIND:
            raise StackError("unsupported_environment_kind")
        if name not in CHECKS_BY_KIND[kind]:
            raise StackError("check_unsupported_for_kind" if name in KNOWN_CHECKS else "unknown_check")
        checks = self._onprem(name, run) if kind == "onprem" else self._cloud(run)
        return checks[name]()

    def _cloud(self, run):
        """aws · gcp: the cluster the stack built, read with the names of its `environment` block."""
        params = run["plan"]["request"]["params"]
        env = substitute(environment_of(run["stack"]), params)
        app, store = env["app_namespace"], env["secret_store"]
        c = self.cluster
        return {
            "cluster-access": lambda: self._cluster_access(env, params),
            "edge-token": lambda: c.secret_exists(app, "bravo-edge-tunnel-token"),
            "secret-store-ready": lambda: c.condition(store["kind"], store.get("namespace"), store["name"], "Ready"),
            "iac-argocd": lambda: c.statefulset_ready(ARGO_NAMESPACE, "bravo-iac-gitops-application-controller"),
            "argocd-repository": lambda: c.labelled_secret_exists(ARGO_NAMESPACE, "argocd.argoproj.io/secret-type=repository"),
            "app-metrics-client": lambda: c.secret_exists(app, "bravo-app-metrics-client"),
            "tailscale-oauth": lambda: c.secret_exists("tailscale-system", "operator-oauth"),
            "tailscale-joined": lambda: TAILSCALE_LOGGED_IN <= set(c.secret_keys("tailscale-system", "operator") or ()),
            "target-checks": lambda: (c.condition("Cluster", app, env["database"], "Ready")
                                      and c.deployments_available(app)
                                      and (params.get("login") != "edge" or login_public(params, self.http))),
            "login-public": lambda: login_public(params, self.http),
            "iac-application-created": lambda: c.application_status(
                f"bravo-iac-{run['plan']['request']['environment_id']}") is not None,
        }

    def _onprem(self, name, run):
        """onprem (RKE2 destination): the T45 checks as they are on develop, with the target-onprem names."""
        params, target = run["plan"]["request"]["params"], run["plan"]["target"]
        c = self.cluster
        node = params.get("node_name")
        # A step that declares `expect` is observable: what it names must be on the node.
        expect = next((s.get("expect") for s in run["stack"]["steps"] if s.get("confirm") == name), None)
        return {
            "target-identity": lambda: c.identity(params["node_name"]) == target and c.node_ready(params["node_name"]),
            "host-paths": lambda: None if not expect else (
                c.node_annotation(node, HOST_PATHS_ANNOTATION) == host_paths_digest(expect["host_paths"])),
            "openbao-image": lambda: None if not expect else set(expected_images(expect, params)) <= c.node_image_names(node),
            "app-image": lambda: set(expected_images(expect, params)) <= c.node_image_names(node),
            "iac-argocd": lambda: c.statefulset_ready(ARGO_NAMESPACE, "bravo-iac-gitops-application-controller"),
            "argocd-repository": lambda: c.labelled_secret_exists(ARGO_NAMESPACE, "argocd.argoproj.io/secret-type=repository"),
            "seaweedfs-auth": lambda: self._seaweedfs_matches_desired(run),
            "app-metrics-client": lambda: c.secret_exists("target-dev", "bravo-app-metrics-client"),
            "tailscale-oauth": lambda: c.secret_exists("tailscale-system", "operator-oauth"),
            # The operator's state Secret gets a current profile only after it logged in to the tailnet
            # (OAuth secret or WIF alike). Synced/Healthy does not show that.
            "tailscale-joined": lambda: TAILSCALE_LOGGED_IN <= set(c.secret_keys("tailscale-system", "operator") or ()),
            "openbao-tls": lambda: c.secret_exists("bravo-secrets", "target-openbao-tls"),
            "openbao-ready": lambda: c.condition("Pod", "bravo-secrets", "target-openbao-0", "Ready"),
            "target-checks": lambda: (c.condition("Cluster", "target-dev", "target-db", "Ready")
                                      and c.deployments_available("target-dev")
                                      and (params.get("login") != "edge" or login_public(params, self.http))),
            "login-public": lambda: login_public(params, self.http),
            "iac-application-created": lambda: c.application_status(
                f"bravo-iac-{run['plan']['request']['environment_id']}") is not None,
            "iac-application-enabled": lambda: _iac_application_on(c, self.repo_root, run),
            # target-exit-map (ADR-0049): the T27 control Pod the residual map's render needs
            "exit-drill-control-ready": lambda: c.condition("Pod", "target-dev", "exit-drill-control", "Ready"),
            # target-onprem-migration: the destination as a migration target
            "audit-metadata-active": lambda: (c.audit_level_events("Metadata") or 0) > 0,
            "iac-network-synced": lambda: _argo_synced(c, self.repo_root, run),
            "replication-credentials": lambda: replication.credentials(c),
            "replication-external-cluster": lambda: replication.external_cluster(c),
            "replication-target-ready": lambda: replication.target_ready(c),
            "initial-copy-complete": lambda: replication.initial_copy(c),
        }

    def _cluster_access(self, env, params):
        """The kubeconfig reaches a cluster whose API server is the one terraform built (endpoint output).

        False (not None) when unreachable: the operator fetches credentials, then confirms again."""
        reference = env.get("cluster_endpoint")
        if reference is None or self.outputs is None:
            return False
        from .terraform import output_path

        try:
            expected = output_path(self.outputs(reference["from"], params), reference)
            server = self.cluster.server()
            self.cluster.cluster_uid()  # reachable with the operator's credentials
        except (StackError, RuntimeError, OSError):
            return False
        return bool(expected) and _host(server) == _host(expected)

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
