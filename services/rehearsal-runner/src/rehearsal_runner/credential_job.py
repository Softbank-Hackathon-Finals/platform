"""Credential-migration verification Jobs: synthetic DB credentials delivered through OpenBao.

The runner generates a synthetic password, a writer Job in the management namespace stores it
under the rehearsal namespace's OpenBao path and creates that namespace's reader role, and the
candidate app reads it through T9's init container. Independent probes then check that the app
reaches the rehearsal-only DB and that another account can neither log in to the reader role nor
read the DB Secret. The cleanup Job removes the role and value; the namespace itself is removed by
the attempt lifecycle. No value is logged, returned or stored in evidence.

OpenBao object names, the role body and the init container are T9's renderer output
(`infra/envs/target-secrets/render.py`), injected through `OpenBaoDelivery`.
"""

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from .credential_candidate import file_diff, prepare_candidate_files
from .credential_proposal_source import BoundCredentialProposalSource
from .credential_verifier import CredentialVerificationRequest
from .recovery_lookup import recovery_identity

CONTROL_NAMESPACE = "bravo-rehearsal-control"
WRITER_ACCOUNT = "rehearsal-openbao-writer"
WRITER_LABEL = {"app.kubernetes.io/component": "rehearsal-openbao-writer"}
READER_ACCOUNT = "rehearsal-eso-reader"
INTRUDER_ACCOUNT = "rehearsal-intruder"
OPENBAO_ADDR = "https://target-openbao.bravo-secrets.svc:8200"
CA_CONFIGMAP = "target-openbao-ca"
DB_SERVICE, DB_SECRET, APP_NAME = "rehearsal-db", "rehearsal-db-auth", "rehearsal-app"
PROBE_SOURCES = "cred-probe-sources"
_IMAGE = re.compile(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}")
_REHEARSAL = re.compile(r"rehearsal-[a-z0-9](?:[-a-z0-9]{0,51}[a-z0-9])?")
_API = {"job": ("apis/batch/v1", "jobs"), "secret": ("api/v1", "secrets"), "configmap": ("api/v1", "configmaps")}

_BAO_ENV = 'export BAO_ADDR="$BAO_ADDR" BAO_CACERT=/openbao/ca/ca.crt; '
_LOGIN = ('JWT=$(cat /var/run/secrets/openbao/token); '
          'TOKEN=$(bao write -field=token auth/kubernetes/login role="$LOGIN_ROLE" jwt="$JWT" 2>/tmp/err) || TOKEN=""; ')
# Each script prints one JSON line and exits 0; the runner judges the observation.
WRITER_SCRIPT = ("set -u; " + _BAO_ENV + 'login=deny; value=failed; role=failed; ' + _LOGIN
                 + 'if [ -n "$TOKEN" ]; then login=allow; export BAO_TOKEN="$TOKEN"; '
                 'bao kv put -mount=secret -cas=0 "$(cat /work/remote)" credentials_json=@/work/synthetic.json >/dev/null 2>&1 && value=ok; '
                 'bao write "auth/kubernetes/role/$(cat /work/role-name)" - < /work/role.json >/dev/null 2>&1 && role=ok; '
                 'bao token revoke -self >/dev/null 2>&1 || true; fi; '
                 'printf \'{"login":"%s","value":"%s","role":"%s"}\\n\' "$login" "$value" "$role"')
CLEANUP_SCRIPT = ("set -u; " + _BAO_ENV + 'login=deny; role=failed; value=failed; ' + _LOGIN
                  + 'if [ -n "$TOKEN" ]; then login=allow; export BAO_TOKEN="$TOKEN"; '
                  'bao delete "auth/kubernetes/role/$(cat /work/role-name)" >/dev/null 2>&1 && role=deleted; '
                  'bao kv metadata delete -mount=secret "$(cat /work/remote)" >/dev/null 2>&1 && value=deleted; '
                  'bao token revoke -self >/dev/null 2>&1 || true; fi; '
                  'printf \'{"login":"%s","role":"%s","value":"%s"}\\n\' "$login" "$role" "$value"')
# Another account in the same namespace tries the reader role. Reachability is checked first so a
# closed network path is never reported as a denial.
INTRUDER_SCRIPT = ("set -u; " + _BAO_ENV + 'reach=no; login=unknown; '
                   'bao status >/dev/null 2>&1; rc=$?; if [ $rc -eq 0 ] || [ $rc -eq 2 ]; then reach=yes; fi; '
                   + _LOGIN + 'if [ -n "$TOKEN" ]; then login=allowed; BAO_TOKEN="$TOKEN" bao token revoke -self >/dev/null 2>&1 || true; '
                   "elif grep -Eq 'Code: (400|403)' /tmp/err; then login=denied; fi; "
                   'printf \'{"reach":"%s","login":"%s"}\\n\' "$reach" "$login"')


class OpenBaoDelivery(Protocol):
    """T9 renderer functions for one rehearsal namespace (no values)."""

    def remote_path(self, namespace: str) -> str: ...
    def role_name(self, namespace: str) -> str: ...
    def auth_role(self, namespace: str) -> dict: ...
    def init_container(self, namespace: str) -> dict: ...


@dataclass(frozen=True)
class CredentialFixture:
    """Images and constants the host pins; the candidate image comes from T7 publication."""

    bao_image: str
    postgres_image: str
    probe_image: str
    openbao_ca_pem: str = field(repr=False)
    # demo-chat (origin/main Dockerfile, app/config.py): port 8080, USER 10001. DB_SECRET_ID and S3_BUCKET
    # are required by the original config; placeholders keep an unrelated KeyError from looking like a
    # DB failure, and a candidate that still calls Secrets Manager fails because egress is closed.
    app_port: int = 8080
    app_user: int = 10001
    database: str = "chat"
    username: str = "rehearsal_app"
    app_env: tuple[tuple[str, str], ...] = (
        ("APP_ENV", "rehearsal"), ("DB_HOST", DB_SERVICE), ("DB_PORT", "5432"), ("DB_NAME", "chat"),
        ("DB_SECRET_ID", "unused-in-rehearsal"), ("S3_BUCKET", "unused-in-rehearsal"),
        ("ATTACHMENT_UPLOAD_ENABLED", "false"))
    # Without a prebuilt `Dockerfile.credential-probe` image the host may pass the runner's own probe
    # modules (file name, source); they are mounted read-only into `probe_image` (a pinned Python base).
    probe_sources: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self):
        if (any(_IMAGE.fullmatch(i or "") is None for i in (self.bao_image, self.postgres_image, self.probe_image))
                or "BEGIN CERTIFICATE" not in self.openbao_ca_pem
                or type(self.app_port) is not int or not 1 <= self.app_port <= 65535
                or type(self.app_user) is not int or not 1 <= self.app_user <= 2**31 - 1
                or re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", self.database) is None
                or re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", self.username) is None
                or any(re.fullmatch(r"[a-z_][a-z0-9_]{0,62}\.py", n) is None or not isinstance(c, str)
                       for n, c in self.probe_sources)
                or (self.probe_sources and "credential_job_probe.py" not in dict(self.probe_sources))):
            raise ValueError("invalid credential fixture")


@dataclass(frozen=True)
class CredentialEvidence:
    verdict: Literal["PASS", "FAIL"]
    reason: str
    namespace: str
    namespace_uid: str
    proposal_digest: str
    diff_hash: str
    image_digest: str
    observations: dict
    executions: dict
    diff: bytes = field(repr=False, compare=False)

    def public(self) -> dict:
        """Evidence fields safe to persist; no credential, token or file content."""
        return {"verdict": self.verdict, "reason": self.reason, "namespace": self.namespace,
                "namespace_uid": self.namespace_uid, "proposal_digest": self.proposal_digest,
                "diff_hash": self.diff_hash, "image_digest": self.image_digest,
                "observations": self.observations, "executions": self.executions}


class VerificationUnknown(RuntimeError):
    """The environment, not the proposal, prevented a definite verdict."""


def judge(obs: dict) -> tuple[Literal["PASS", "FAIL"], str]:
    """Definite verdict only when every control held; isolation breaches are not the proposal's fault."""
    if obs.get("writer") != {"login": "allow", "value": "ok", "role": "ok"}:
        raise VerificationUnknown("writer_unconfirmed")
    if obs.get("db_ready") != "yes" or obs.get("secret_before") != "yes" or obs.get("secret_after") != "yes":
        raise VerificationUnknown("fixture_unconfirmed")
    if obs.get("init") != "succeeded":
        raise VerificationUnknown("delivery_unconfirmed")  # T9's init container, not the candidate
    if obs.get("app") == "crashed" or obs.get("app_db") == "no":
        return "FAIL", "APP_DB_CONNECTION_FAILED"
    if obs.get("app") != "ready" or obs.get("app_db") != "yes":
        raise VerificationUnknown("app_unconfirmed")
    if obs.get("intruder") != {"reach": "yes", "login": "denied"} or obs.get("intruder_secret_read") != "denied":
        raise VerificationUnknown("isolation_unconfirmed")
    return "PASS", "NONE"


@dataclass(frozen=True)
class CredentialJob:
    """Product verification for one credential attempt; `kube` is T38's `verification.Kube`.

    `kubectl` is the runner's `isolation.kube.Kubectl`, used only for the impersonated
    Secret read whose denial message must name the tested account.
    `candidate_image(diff_hash)` returns the T7-published image for exactly that diff.
    """

    kube: object = field(repr=False)
    kubectl: object = field(repr=False)
    proposals: BoundCredentialProposalSource = field(repr=False)
    snapshot: object = field(repr=False)
    delivery: OpenBaoDelivery = field(repr=False)
    fixture: CredentialFixture
    candidate_image: Callable[[str], str] = field(repr=False)
    poll_seconds: float = 2.0
    deadline_seconds: int = 180
    password: Callable[[], str] = field(default=lambda: secrets.token_urlsafe(24), repr=False)
    observe: Callable | None = field(default=None, repr=False)  # T38 observe_job_pod; injectable for tests

    def verify(self, request: CredentialVerificationRequest) -> CredentialEvidence:
        namespace = request.namespace
        self._require_namespace(namespace)
        edits = self.proposals.resolve(request)
        files = prepare_candidate_files(self.snapshot, edits)
        diff = b"".join(file_diff(f.path, self.snapshot.read(f.path), f.content) for f in files)
        if not diff:
            raise ValueError("candidate contains no changes")
        diff_hash = "sha256:" + hashlib.sha256(diff).hexdigest()
        image = self.candidate_image(diff_hash)
        if not isinstance(image, str) or _IMAGE.fullmatch(image) is None:
            raise VerificationUnknown("candidate_image_unpublished")
        labels, annotations = recovery_identity(request.run_id, request.attempt)
        ns = namespace.name
        control = f"t15-cred-{ns}"
        rmeta = {"namespace": ns, "labels": labels, "annotations": annotations}
        cmeta = {"namespace": CONTROL_NAMESPACE, "labels": labels, "annotations": annotations}
        obs, executions, created = {}, {}, []
        try:
            password = self.password()
            self._control_objects(ns, control, cmeta, created, password)
            obs["writer"], executions["writer"] = self._job(
                CONTROL_NAMESPACE, control + "-write", self.fixture.bao_image, WRITER_SCRIPT, WRITER_ACCOUNT,
                cmeta, {"login", "value", "role"}, work=control, role=WRITER_ACCOUNT, labels=WRITER_LABEL)
            self._rehearsal_fixtures(ns, rmeta, password)
            del password
            obs["secret_before"] = self._secret_exists(ns)
            obs["db_ready"] = "yes" if self._watch_pod(ns, DB_SERVICE, self.fixture.postgres_image)[0] == "ready" else "no"
            obs["app"], obs["init"], executions["app"] = self._start_app(ns, rmeta, image)
            if obs["app"] == "ready":
                if self.fixture.probe_sources:
                    self.kube.create({"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                                      "metadata": rmeta | {"name": PROBE_SOURCES},
                                      "data": dict(self.fixture.probe_sources)})
                result, executions["app_db"] = self._job(
                    ns, "cred-app-db", self.fixture.probe_image, None, "default", rmeta, {"observation"},
                    command=["python", "-m", "rehearsal_runner.credential_job_probe", "app-db", "--namespace", ns,
                             "--service", APP_NAME, "--port", str(self.fixture.app_port)],
                    probe_sources=bool(self.fixture.probe_sources))
                obs["app_db"] = result["observation"]
            obs["intruder"], executions["intruder"] = self._job(
                ns, "cred-intruder", self.fixture.bao_image, INTRUDER_SCRIPT, INTRUDER_ACCOUNT, rmeta,
                {"reach", "login"}, role=self.delivery.role_name(ns))
            obs["intruder_secret_read"] = self._impersonated_secret_read(ns)
            obs["secret_after"] = self._secret_exists(ns)
            self._require_namespace(namespace)
            verdict, reason = judge(obs)
        finally:
            obs["cleanup"] = self._cleanup(control, cmeta, created)
        if obs["cleanup"] != {"login": "allow", "role": "deleted", "value": "deleted"}:
            raise VerificationUnknown("openbao_cleanup_unconfirmed")
        return CredentialEvidence(verdict, reason, ns, namespace.uid, request.proposal_digest, diff_hash,
                                  "sha256:" + image.split("@sha256:")[1], obs, executions, diff)

    # --- objects -------------------------------------------------------------------------------

    def _require_namespace(self, namespace) -> None:
        meta = self.kube.get("namespace", namespace.name)["metadata"]
        if meta.get("uid") != namespace.uid or meta.get("deletionTimestamp"):
            raise VerificationUnknown("namespace_identity_changed")

    def _control_objects(self, ns: str, control: str, meta: dict, created: list, password: str) -> None:
        """The writer Job's inputs: OpenBao names from T9 and the synthetic value (Secret only)."""
        synthetic = json.dumps({"username": self.fixture.username, "password": password}, separators=(",", ":"))
        role = json.dumps(self.delivery.auth_role(ns), sort_keys=True, separators=(",", ":"))
        for manifest in (
            {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": meta | {"name": control},
             "stringData": {"remote": self.delivery.remote_path(ns), "role-name": self.delivery.role_name(ns),
                            "role.json": role, "synthetic.json": synthetic}},
            {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True, "metadata": meta | {"name": control + "-ca"},
             "data": {"ca.crt": self.fixture.openbao_ca_pem}},
        ):
            uid = self.kube.create(manifest)["metadata"]["uid"]
            created.append((manifest["kind"].lower(), manifest["metadata"]["name"], uid))

    def _rehearsal_fixtures(self, ns: str, meta: dict, password: str) -> None:
        """Accounts, CA, and a rehearsal-only Postgres using the same synthetic credentials."""
        f = self.fixture
        for account in (READER_ACCOUNT, INTRUDER_ACCOUNT):
            self.kube.create({"apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
                              "metadata": meta | {"name": account}})
        self.kube.create({"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
                          "metadata": meta | {"name": CA_CONFIGMAP}, "data": {"ca.crt": f.openbao_ca_pem}})
        self.kube.create({"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": meta | {"name": DB_SECRET},
                          "stringData": {"POSTGRES_USER": f.username, "POSTGRES_PASSWORD": password,
                                         "POSTGRES_DB": f.database}})
        self.kube.create(_pod(meta, DB_SERVICE, "default", {
            "name": "run", "image": f.postgres_image, "ports": [{"containerPort": 5432}],
            "envFrom": [{"secretRef": {"name": DB_SECRET}}],
            "env": [{"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"}],
            "readinessProbe": {"exec": {"command": ["pg_isready", "-U", f.username, "-d", f.database]}, "periodSeconds": 2},
            "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"},
                             {"name": "run", "mountPath": "/var/run/postgresql"}]},
            [{"name": "data", "emptyDir": {}}, {"name": "run", "emptyDir": {}}], user=999, writable=True))
        self.kube.create(_service(meta, DB_SERVICE, 5432))

    def _start_app(self, ns: str, meta: dict, image: str) -> tuple[str, str, dict]:
        """Candidate app with T9's init container and the three agreed volumes (T9 11/06 01:23)."""
        f = self.fixture
        init = {k: v for k, v in self.delivery.init_container(ns).items() if not k.startswith("x-")}
        app = _pod(meta, APP_NAME, READER_ACCOUNT, {
            "name": "run", "image": image, "ports": [{"containerPort": f.app_port}],
            "env": [{"name": k, "value": v} for k, v in f.app_env],
            "volumeMounts": [{"name": "bravo-database", "mountPath": "/run/bravo/database", "readOnly": True},
                             {"name": "tmp", "mountPath": "/tmp"}]},
            [{"name": "bravo-database", "emptyDir": {"medium": "Memory"}}, {"name": "tmp", "emptyDir": {}},
             {"name": "openbao-ca", "configMap": {"name": CA_CONFIGMAP}},
             {"name": "openbao-token", "projected": {"sources": [{"serviceAccountToken": {
                 "audience": "openbao", "expirationSeconds": 600, "path": "token"}}]}}], user=f.app_user)
        app["spec"]["initContainers"] = [init]
        self.kube.create(app)
        self.kube.create(_service(meta, APP_NAME, f.app_port))
        state, record = self._watch_pod(ns, APP_NAME, image, init=True)
        return state, record.pop("init"), record

    def _watch_pod(self, ns: str, name: str, image: str, *, init: bool = False) -> tuple[str, dict]:
        """ready / crashed / init_failed / unknown, from status only (no container logs)."""
        uid, record = self.kube.get("pod", name, ns)["metadata"]["uid"], {"init": "unknown"}

        def state():
            pod = self.kube.get("pod", name, ns)
            if pod["metadata"]["uid"] != uid:
                raise VerificationUnknown("pod_identity_changed")
            status = pod.get("status", {})
            if init:
                first = (status.get("initContainerStatuses") or [{}])[0]
                done = first.get("state", {}).get("terminated")
                if done or first.get("restartCount", 0):
                    record["init"] = "succeeded" if done and done.get("exitCode") == 0 else "failed"
                if record["init"] == "failed":
                    return "init_failed"
            run = (status.get("containerStatuses") or [{}])[0]
            if run.get("restartCount", 0) or run.get("state", {}).get("terminated"):
                return "crashed"
            if run.get("ready") is True and run.get("imageID", "").split("@")[-1] == image.split("@")[-1]:
                record.update(pod_uid=uid, image_id=run["imageID"], container_id=run.get("containerID"))
                return "ready"
            return None

        return self._wait(state) or "unknown", record

    def _job(self, namespace: str, name: str, image: str, script: str | None, account: str, meta: dict,
             keys: set, *, work: str | None = None, role: str | None = None, labels: dict | None = None,
             command: list | None = None, probe_sources: bool = False) -> tuple[dict, dict]:
        observe_job_pod = self.observe
        if observe_job_pod is None:
            from bravo_agent.verification import observe_job_pod

        if namespace != CONTROL_NAMESPACE and _REHEARSAL.fullmatch(namespace) is None:
            raise ValueError("job outside the rehearsal or control namespace")
        env = [{"name": "BAO_ADDR", "value": OPENBAO_ADDR}, {"name": "HOME", "value": "/tmp"}]
        if role:
            env.append({"name": "LOGIN_ROLE", "value": role})
        mounts, volumes = [{"name": "tmp", "mountPath": "/tmp"}], [{"name": "tmp", "emptyDir": {}}]
        if script is not None:  # OpenBao jobs log in with a projected token of `account`
            mounts += [{"name": "openbao-ca", "mountPath": "/openbao/ca", "readOnly": True},
                       {"name": "openbao-token", "mountPath": "/var/run/secrets/openbao", "readOnly": True}]
            volumes += [{"name": "openbao-ca", "configMap": {"name": (work + "-ca") if work else CA_CONFIGMAP}},
                        {"name": "openbao-token", "projected": {"sources": [{"serviceAccountToken": {
                            "audience": "openbao", "expirationSeconds": 600, "path": "token"}}]}}]
        if probe_sources:
            env.append({"name": "PYTHONPATH", "value": "/probe"})
            mounts.append({"name": "probe", "mountPath": "/probe/rehearsal_runner", "readOnly": True})
            volumes.append({"name": "probe", "configMap": {"name": PROBE_SOURCES}})
        if work:
            mounts.append({"name": "work", "mountPath": "/work", "readOnly": True})
            volumes.append({"name": "work", "secret": {"secretName": work}})
        container = {"name": "run", "image": image, "imagePullPolicy": "IfNotPresent",
                     "command": command or ["/bin/sh", "-c", script], "env": env, "volumeMounts": mounts,
                     "resources": {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
                     "securityContext": _container_security()}
        manifest = {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta | {"name": name},
                    "spec": {"backoffLimit": 0, "activeDeadlineSeconds": self.deadline_seconds, "template": {
                        "metadata": {"labels": {"role": "credential-verifier", **(labels or {})}},
                        "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                 "serviceAccountName": account, "securityContext": _pod_security(65532),
                                 "volumes": volumes, "containers": [container]}}}}
        created = self.kube.create(manifest)
        uid = created["metadata"]["uid"]
        if not self._wait(lambda: _job_done(self.kube.get("job", name, namespace), uid)):
            raise VerificationUnknown("job_timeout")
        execution = observe_job_pod(self.kube, created, image, complete=True)
        log = self.kube.call("logs", "pod/" + execution["pod_name"], "-n", namespace, "-c", "run", "--limit-bytes=2048")
        if observe_job_pod(self.kube, created, image, complete=True) != execution:
            raise VerificationUnknown("job_execution_changed")
        try:
            observation = json.loads(log.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise VerificationUnknown("invalid_observation") from None
        if (not isinstance(observation, dict) or set(observation) != keys
                or any(not isinstance(v, str) for v in observation.values())):
            raise VerificationUnknown("invalid_observation")
        record = {k: execution[k] for k in ("job_uid", "pod_uid", "container_id", "image_id", "exit_code")}
        record["log_sha256"] = hashlib.sha256(log.encode("utf-8")).hexdigest()
        return observation, record

    def _secret_exists(self, ns: str) -> str:
        try:
            return "yes" if self.kube.get("secret", DB_SECRET, ns)["metadata"].get("uid") else "unknown"
        except Exception:
            return "unknown"

    def _impersonated_secret_read(self, ns: str) -> str:
        """Denied only when the API server names the intruder account (an ignored --as proves nothing)."""
        account = f"system:serviceaccount:{ns}:{INTRUDER_ACCOUNT}"
        who = self.kubectl.run("auth", "whoami", "--as", account, "-o", "jsonpath={.status.userInfo.username}")
        if who.returncode != 0 or who.stdout.strip() != account:
            return "unknown"
        out = self.kubectl.run("get", "secret", DB_SECRET, "-n", ns, "--as", account, "-o", "name")
        if out.returncode == 0:
            return "allowed"
        return "denied" if "Forbidden" in out.stderr and f'User "{account}"' in out.stderr else "unknown"

    def _cleanup(self, control: str, meta: dict, created: list) -> dict:
        """Remove the role and value, then this attempt's control objects by UID. Never raises."""
        if not created:
            return {"login": "allow", "role": "deleted", "value": "deleted"}  # nothing reached OpenBao
        try:
            result, _ = self._job(CONTROL_NAMESPACE, control + "-cleanup", self.fixture.bao_image, CLEANUP_SCRIPT,
                                  WRITER_ACCOUNT, meta, {"login", "role", "value"}, work=control,
                                  role=WRITER_ACCOUNT, labels=WRITER_LABEL)
        except Exception:
            result = {"login": "unknown", "role": "unknown", "value": "unknown"}
        jobs = []
        for name in (control + "-write", control + "-cleanup"):
            try:
                jobs.append(("job", name, self.kube.get("job", name, CONTROL_NAMESPACE)["metadata"]["uid"]))
            except Exception:
                continue
        for kind, name, uid in jobs + created:
            try:
                group, plural = _API[kind]
                self.kube.call("delete", "--raw", f"/{group}/namespaces/{CONTROL_NAMESPACE}/{plural}/{name}", "-f", "-",
                               data={"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid},
                                     "propagationPolicy": "Background"})
            except Exception:
                result = {**result, "control_objects": "unconfirmed"}
        return result

    def _wait(self, check):
        deadline = time.monotonic() + self.deadline_seconds + 30
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(self.poll_seconds)
        return None


def _job_done(job: dict, uid: str) -> bool:
    if job["metadata"]["uid"] != uid:
        raise VerificationUnknown("job_identity_changed")
    conditions = {c.get("type") for c in job.get("status", {}).get("conditions", []) if c.get("status") == "True"}
    if "Failed" in conditions:
        raise VerificationUnknown("job_failed")
    return "Complete" in conditions


def _container_security() -> dict:
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}


def _pod_security(user: int) -> dict:
    return {"runAsNonRoot": True, "runAsUser": user, "runAsGroup": user, "fsGroup": user,
            "seccompProfile": {"type": "RuntimeDefault"}}


def _pod(meta: dict, name: str, account: str, container: dict, volumes: list, *,
         user: int = 65532, writable: bool = False) -> dict:
    """A restricted-PSA Pod; Postgres needs a writable root filesystem, nothing else does."""
    container = {"imagePullPolicy": "IfNotPresent", **container,
                 "securityContext": _container_security() | ({"readOnlyRootFilesystem": False} if writable else {})}
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {**meta, "name": name, "labels": {**meta["labels"], "app": name}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False, "serviceAccountName": account,
                     "enableServiceLinks": False, "securityContext": _pod_security(user),
                     "containers": [container], "volumes": volumes}}


def _service(meta: dict, name: str, port: int) -> dict:
    return {"apiVersion": "v1", "kind": "Service", "metadata": meta | {"name": name},
            "spec": {"type": "ClusterIP", "selector": {"app": name}, "ports": [{"port": port, "targetPort": port}]}}
