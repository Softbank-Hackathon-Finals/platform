"""Real ports for the executor: run records, the configuration repository, the cluster, T10.

Every external call is a fixed argument list (no shell). Secret values travel only on stdin to
`kubectl create`; they are never in arguments, files or logs. The repository writer never force
pushes: a moved remote stops the step.
"""

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .applications import ARGO_NAMESPACE
from .stack import StackError

TIMEOUT = 600


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
        path = self.root / f"{plan_id}.json"
        if path.exists():
            raise StackError("plan_exists")
        path.write_text(json.dumps(value, sort_keys=True))
        os.chmod(path, 0o600)

    def load(self, plan_id):
        path = self.root / f"{plan_id}.json"
        if not plan_id.replace("-", "").isalnum() or not path.exists():
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

    def _git(self, *args, check=True, raw=False):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "LANG": "C"} | self.AUTHOR
        if "SSH_AUTH_SOCK" in os.environ:
            env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        out = _run([self.git, *args], cwd=self.workdir, env=env, check=check).stdout
        return out if raw else out.strip()

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
        if self.head() != expected_head:
            raise StackError("config_repo_moved")
        self._git("checkout", "-q", "-B", "work", expected_head)
        target = self.workdir / path
        if target.exists():
            shutil.rmtree(target)  # Git only; prune is off, so no cluster resource is deleted
        for name, text in files.items():
            file = self.workdir / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text)
        self._git("add", "-A", "--", path)
        self._git("commit", "-q", "-m", message)
        commit = self._git("rev-parse", "HEAD")
        pushed = _run([self.git, "push", "-q", "origin", f"{commit}:refs/heads/{self.branch}"],
                      cwd=self.workdir, check=False,
                      env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")})
        if pushed.returncode != 0:
            raise StackError("config_repo_moved")  # non-fast-forward; never force
        return commit


class KubectlCluster:
    """kubectl and helm against one destination cluster (kubeconfig given by the operator)."""

    FIELD_MANAGER = "bravo-infra-plan"

    def __init__(self, kubeconfig, *, kubectl="kubectl", helm="helm", context=None):
        self.kubeconfig, self.kubectl, self.helm, self.context = str(kubeconfig), kubectl, helm, context

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
            return True  # cannot compare (e.g. new CRD kind): apply decides
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

    def helm_digest(self, release, namespace):
        result = _run([self.helm, "history", release, "-n", namespace, "--max", "1", "-o", "json"],
                      env=self._env(), check=False)
        if result.returncode != 0:
            return None
        history = json.loads(result.stdout or "[]")
        description = history[-1].get("description", "") if history else ""
        status = history[-1].get("status") if history else None
        prefix = "bravo-digest="
        return description[len(prefix):] if description.startswith(prefix) and status == "deployed" else None

    def helm_install(self, chart_dir, values_file, *, release, namespace, digest):
        with tempfile.TemporaryDirectory() as scratch:
            chart = Path(scratch) / "chart"
            shutil.copytree(chart_dir, chart, ignore=shutil.ignore_patterns("charts"))
            _run([self.helm, "dependency", "build", str(chart)], env=self._env())
            _run([self.helm, "upgrade", "--install", release, str(chart), "-n", namespace, "--create-namespace",
                  "-f", str(values_file), "--description", f"bravo-digest={digest}", "--wait",
                  "--timeout", "10m"], env=self._env())


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
            "seaweedfs-auth": lambda: c.secret_exists("target-dev", "target-seaweedfs-auth"),
            "tailscale-oauth": lambda: c.secret_exists("tailscale-system", "operator-oauth"),
            "openbao-tls": lambda: c.secret_exists("bravo-secrets", "target-openbao-tls"),
            "openbao-ready": lambda: c.condition("Pod", "bravo-secrets", "target-openbao-0", "Ready"),
            "target-checks": lambda: (c.condition("Cluster", "target-dev", "target-db", "Ready")
                                      and c.deployments_available("target-dev")),
        }
        if name not in checks:
            raise StackError("unknown_check")
        return checks[name]()
