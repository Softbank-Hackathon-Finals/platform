"""P stage: build the destination with T45's target-onprem stack, no person in the loop.

The T45 executor runs the approved plan. Where it stops for a person (`operator_action`), the platform
does that step's work in code and resumes; T45 then observes the result with its own confirm check and
records SKIPPED. Nothing runs outside the approved run's order, and the stack is not changed to get there.

    step                what the platform does here (the person's instruction in stack.json)
    host-dirs           site host creates the declared directories; node annotation binds them
    openbao-image       site host imports the verified OCI archive under its tag and digest names
    app-image           the same for the destination app (arm64: local build; amd64: the pinned GHCR index)
    argocd              helm installs T44's customer Argo CD (infra/iac/argocd), install-only
    config-repo-access  registers the destination configuration repository, read-only (a hosted one with a
                        deploy key the platform generates and registers read-only)
    object-secrets      renders s3.json for the plan's desired exposure (T44 state via T45) into the Secret
    openbao-tls         creates the OpenBao CA and leaf in memory; the CA key stays with the platform
    openbao-init        init once, unseal, KV v2 (CAS), Kubernetes auth, policies, source values (CAS 0)
    app-metrics-client  T37 app client certificate from this destination's app CA (the CA key stays with the platform)

Secret values exist in memory, in the platform vault (vault.py) and in the cluster only.
"""

import atexit
import base64
import importlib.util
import ipaddress
import json
import os
import secrets
import string
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bravo_infra_plan import Planner, load_stack
from bravo_infra_plan.adapters import (
    HOST_PATHS_ANNOTATION,
    ClusterChecks,
    FileRunStore,
    GitConfigRepo,
    KubectlCluster,
    T10Approvals,
    expected_images,
    host_paths_digest,
)
from bravo_infra_plan.executor import Executor
from bravo_infra_plan.plan import HelmChartRenderer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from bravo_migration.kube import Kube, KubeError
from bravo_migration.vault import PlatformVault

BAO_NS, BAO_POD, APP_NS, ARGO_NS = "bravo-secrets", "target-openbao-0", "target-dev", "bravo-iac-gitops"
TERMINAL = ("SUCCEEDED", "REFUSED", "FAILED")
STRICTER_THAN_CHECK = ("openbao-init",)


class DestinationError(Exception):
    def __init__(self, code: str, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.code, self.detail = code, detail or {}


def _random(n: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _ssh_keypair() -> dict:
    """An Ed25519 deploy key: OpenSSH private key for Argo CD, public key for the repository."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    return {"private": private, "public": public}


def _register_deploy_key(repository: str, title: str, public: str, *, read_only: bool, gh: str = "gh") -> None:
    """Add the deploy key to the GitHub repository once. Only the public key leaves the platform; a key already
    registered with other access than asked is refused, never changed."""
    listed = subprocess.run([gh, "api", f"repos/{repository}/keys"], capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        raise DestinationError("config_repo_keys_unreadable", "the repository deploy keys could not be read")
    for existing in json.loads(listed.stdout):
        if existing["key"].split()[:2] == public.split()[:2]:
            if existing["read_only"] != read_only:
                raise DestinationError("config_repo_key_writable" if read_only else "config_repo_key_read_only",
                                       "the registered deploy key can write" if read_only
                                       else "the registered push key is read-only")
            return
    body = json.dumps({"title": title, "key": public, "read_only": read_only})
    added = subprocess.run([gh, "api", "-X", "POST", f"repos/{repository}/keys", "--input", "-"], input=body,
                           capture_output=True, text=True, check=False)
    if added.returncode != 0:
        raise DestinationError("config_repo_key_unregistered", "the deploy key could not be registered")


def _register_read_only_key(repository: str, title: str, public: str, gh: str = "gh") -> None:
    _register_deploy_key(repository, title, public, read_only=True, gh=gh)


def _push_agent(private: str) -> dict:
    """A run-scoped ssh-agent holding the platform's push key for one configuration repository. The key goes in
    on stdin; GitConfigRepo passes only SSH_AUTH_SOCK to git (no user git config, no other key)."""
    out = subprocess.run(["ssh-agent", "-s"], capture_output=True, text=True, check=True).stdout
    env = dict(line.split(";", 1)[0].split("=", 1) for line in out.splitlines() if "=" in line.split(";", 1)[0])
    added = subprocess.run(["ssh-add", "-q", "-"], input=private, capture_output=True, text=True, check=False,
                           env={**os.environ, "SSH_AUTH_SOCK": env["SSH_AUTH_SOCK"]})
    if added.returncode != 0:
        subprocess.run(["kill", env["SSH_AGENT_PID"]], check=False)
        raise DestinationError("config_repo_push_key_unloaded", "the push key could not be loaded")
    return env


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Destination:
    """One destination cluster and one T45 run. `token` returns a fresh platform token for T10."""

    def __init__(self, cfg: dict, *, host, vault: PlatformVault, token: Callable[[], str],
                 approve: Callable[[dict], str], source_values: Callable[[], dict], log=print):
        self.cfg, self.host, self.vault, self.token = cfg, host, vault, token
        self.approve, self.source_values, self.log = approve, source_values, log
        self.repo_root = Path(cfg["repo_root"]).resolve()
        self.stack = load_stack(self.repo_root / "infra/stacks/target-onprem/stack.json")
        self.params = cfg["params"]
        self.node = self.params["node_name"]
        self.kube = Kube(cfg["kubeconfig"], cfg["cluster_uid"])
        self.cluster = KubectlCluster(cfg["kubeconfig"])
        self.revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo_root, check=True,
                                       capture_output=True, text=True).stdout.strip()
        repo = cfg["config_repo"]
        if repo.get("push_credential") == "deploy-key":
            # ADR-0033: the plan API writes the destination's configuration repository. The platform holds a write
            # deploy key scoped to this one repository and lends it to git through a run-scoped agent.
            key = vault.put_once("config-repo-push-key", _ssh_keypair())
            _register_deploy_key(repo["github_repo"], f"bravo-platform-push-{cfg['environment_id']}", key["public"],
                                 read_only=False)
            self.push_agent = _push_agent(key["private"])
            atexit.register(subprocess.run, ["kill", self.push_agent["SSH_AGENT_PID"]], check=False)
            os.environ["SSH_AUTH_SOCK"] = self.push_agent["SSH_AUTH_SOCK"]
        self.repo = GitConfigRepo(repo["remote"], repo.get("branch", "main"), repo["workdir"])
        self.runs = FileRunStore(cfg["runs_dir"])
        self.actions = {
            "host-dirs": self.host_dirs, "openbao-image": self.openbao_image, "app-image": self.app_image,
            "argocd": self.argocd,
            "config-repo-access": self.config_repo_access, "object-secrets": self.object_secrets,
            "openbao-tls": self.openbao_tls, "openbao-init": self.openbao_init,
            "app-metrics-client": self.app_metrics_client,
        }

    # --- the T45 run -------------------------------------------------------------------------------

    def request(self) -> dict:
        return {"stack": self.stack.name, "environment_id": self.cfg["environment_id"], "params": self.params}

    def _executor(self) -> Executor:
        planner = Planner(self.stack, self.repo_root, chart_renderer=HelmChartRenderer(self.cfg["charts_dir"]))
        return Executor(planner, stack_revision=self.revision, cluster=self.cluster, repo=self.repo,
                        approvals=T10Approvals(self.cfg["t10"]["url"], self.token()), checks=ClusterChecks(self.cluster),
                        store=self.runs, repo_url=self.cfg["config_repo"]["argo_url"], ledger=self._ledger)

    def _ledger(self, event: dict) -> None:
        with open(Path(self.cfg["runs_dir"]) / "ledger.jsonl", "a") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")

    def run(self, run_id: str, *, max_rounds: int = 30) -> dict:
        self.kube.verify_identity()
        executor = self._executor()
        if self.runs.exists(run_id):
            run = executor.resume(run_id)
        else:
            target = self.cluster.identity(self.node)
            plan = executor.planner.plan(self.request(), stack_revision=self.revision, target=target,
                                         base_commit=self.repo.head())
            binding = {"infra_plan_id": f"{run_id}-plan", "plan_digest": plan.plan_digest}
            approval_id = self.approve(binding)
            self.log(f"plan {plan.plan_digest[:12]} approved ({approval_id}); omitted: "
                     f"{[s['id'] for s in plan.document['steps'] if s.get('omitted')]}")
            run = executor.start(request=self.request(), migration_plan_id=self.cfg["t10"]["plan_id"],
                                 approval_id=approval_id, run_id=run_id, started_by="migration-driver")
        adapter_retries = 0
        for _ in range(max_rounds):
            self._report(run)
            if run["status"] in TERMINAL:
                return run
            # T45 confirms openbao-init by Pod Ready only; the KV values, auth and policies behind it are this
            # action's own completion. A step T45 already passed without it is finished here, then re-observed.
            unfinished = [s for s in STRICTER_THAN_CHECK if run["steps"][s]["status"] in ("SKIPPED", "APPLIED")
                          and self.vault.get(f"done-{s}") is None]
            if unfinished:
                for step in unfinished:
                    self.log(f"  finishing {step}: T45 passed it before the platform action completed")
                    self.actions[step](run)
                run = self._executor().resume(run_id)
                continue
            held = {s: v.get("reason") for s, v in run["steps"].items() if v["status"] == "HOLD"}
            if run["status"] == "HOLD" or held:
                # Only an adapter error (kubectl/helm could not answer) is re-observed, a bounded number of
                # times; resume re-reads every held step and never re-applies a finished one. Any other
                # hold is a verdict this driver does not override.
                if held and set(held.values()) == {"adapter_error"} and adapter_retries < 3:
                    adapter_retries += 1
                    self.log(f"  re-observing after adapter error ({adapter_retries}/3): {sorted(held)}")
                    time.sleep(5 * adapter_retries)
                    run = self._executor().resume(run_id)
                    continue
                return run
            waiting = [s for s, v in run["steps"].items() if v["status"] == "WAITING_OPERATOR"]
            if not waiting:
                return run
            for step in waiting:
                action = self.actions.get(step)
                if action is None:
                    raise DestinationError("operator_step_unhandled", f"no platform action for {step}")
                started = time.monotonic()
                action(run)
                self.log(f"  platform action {step}: done in {time.monotonic() - started:.1f}s")
            run = self._executor().resume(run_id)
        return run

    def _report(self, run: dict) -> None:
        counts: dict[str, int] = {}
        for v in run["steps"].values():
            counts[v["status"]] = counts.get(v["status"], 0) + 1
        notable = {k: v.get("reason") + (f" ({v['error']})" if v.get("error") else "") for k, v in run["steps"].items() if v["status"] in ("WAITING_OPERATOR", "HOLD", "FAILED")}
        self.log(f"run {run['run_id']}: {run['status']} {counts} {notable or ''}")

    def _expect(self, step_id: str) -> dict:
        return self.stack.steps[step_id]["expect"]

    # --- platform actions --------------------------------------------------------------------------

    def host_dirs(self, run: dict) -> None:
        paths = self._expect("host-dirs")["host_paths"]
        digest = host_paths_digest(paths)
        current = self.cluster.node_annotation(self.node, HOST_PATHS_ANNOTATION)
        if current not in (None, digest):
            raise DestinationError("host_paths_attested_differently", "the node carries another host-paths attestation")
        self.host.prepare_paths(paths)
        self.kube.run("annotate", "node", self.node, f"{HOST_PATHS_ANNOTATION}={digest}", "--overwrite")

    def openbao_image(self, run: dict) -> None:
        self._import_image("openbao-image", self.cfg["images"]["openbao"])

    def app_image(self, run: dict) -> None:
        self._import_image("app-image", self.cfg["images"]["app"])

    def _import_image(self, step: str, oci_tar: str) -> None:
        names = expected_images(self._expect(step), self.params)
        self.host.import_image(oci_tar, names)
        deadline = time.monotonic() + 120
        while not set(names) <= self.cluster.node_image_names(self.node):
            if time.monotonic() > deadline:
                raise DestinationError("image_not_reported", "the kubelet does not report the imported image yet")
            time.sleep(5)

    def argocd(self, run: dict) -> None:
        chart = self.repo_root / "infra/iac/argocd"
        a = self.cfg["argocd"]
        subprocess.run(["helm", "dependency", "build", str(chart)], check=True, capture_output=True)
        values = ["--set", f"environment={a['environment']}", "--set", f"tailnet={a['tailnet']}",
                  "--set", f"targetRevision={a['target_revision']}", "--set", "application.enabled=false",
                  "--set", "application.autoSync=false", "--set", "application.prune=false"]
        for _ in range(2):  # the stack's instruction: two installs (CRDs first, then what uses them)
            subprocess.run(["helm", "upgrade", "--install", "bravo-iac-gitops", str(chart), "-n", ARGO_NS,
                            "--create-namespace", "--kubeconfig", self.cfg["kubeconfig"], *values,
                            "--wait", "--timeout", "10m"], check=True, capture_output=True)

    def config_repo_access(self, run: dict) -> None:
        repo = self.cfg["config_repo"]
        data = {"type": "git", "url": repo["argo_url"]}
        # The local configuration repository is served read-only over HTTP and needs no credential. A hosted
        # one (ADR-0033: one private repository per destination) is read with a deploy key the platform
        # generates once, keeps in its vault and registers read-only; the private key goes to Argo CD only.
        if repo.get("credential") == "deploy-key":
            key = self.vault.put_once("config-repo-deploy-key", _ssh_keypair())
            _register_read_only_key(repo["github_repo"], f"bravo-argocd-{self.cfg['environment_id']}", key["public"])
            data["sshPrivateKey"] = key["private"]
        elif repo.get("credential"):
            raise DestinationError("config_repo_credential_unsupported", "only a read-only deploy key is supported")
        self.kube.apply([{"apiVersion": "v1", "kind": "Secret", "metadata": {
            "name": "bravo-destination-config", "namespace": ARGO_NS,
            "labels": {"argocd.argoproj.io/secret-type": "repository"}}, "stringData": data}])

    def object_secrets(self, run: dict) -> None:
        # The values come from T44's desired state as the approved plan bound them (T45 `desired`, #642).
        exposure = next(e for e in run["plan"]["steps"] if e["id"] == "object-secrets")["desired"]
        identities = self.vault.put_once("seaweedfs-identities", {
            name: {"access_key": "BRAVO" + _random(15).upper(), "secret_key": _random(40)}
            for name in ("bootstrap", "client")})
        env = {"PATH": os.environ.get("PATH", "")}
        for name, ident in identities.items():
            env[f"BRAVO_OBJECT_{name.upper()}_ACCESS_KEY"] = ident["access_key"]
            env[f"BRAVO_OBJECT_{name.upper()}_SECRET_KEY"] = ident["secret_key"]
        args = ["--state", exposure["state"], "--bucket", exposure["bucket"]]
        if exposure["state"] == "open-demo":
            args += ["--acknowledge-anonymous-read", "--demo-run-id", exposure["demo_run_id"]]
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            out = Path(tmp) / "s3.json"
            subprocess.run(["python3", str(self.repo_root / "infra/envs/target-workloads/object_exposure.py"),
                            "render", *args, "--output", str(out)], env=env, check=True, capture_output=True)
            s3 = out.read_text()
        self._create_secret(APP_NS, "target-seaweedfs-auth", {"s3.json": s3})

    def openbao_tls(self, run: dict) -> None:
        material = self.vault.get("openbao-tls")
        if material is None:
            material = self.vault.put_once("openbao-tls", _openbao_tls())
        self._create_secret(BAO_NS, "target-openbao-tls", {
            "tls.crt": material["leaf_crt"], "tls.key": material["leaf_key"], "ca.crt": material["ca_crt"]},
            secret_type="kubernetes.io/tls")
        self.kube.apply([{"apiVersion": "v1", "kind": "ConfigMap",
                          "metadata": {"name": "target-openbao-ca", "namespace": APP_NS},
                          "data": {"ca.crt": material["ca_crt"]}}])

    def openbao_init(self, run: dict) -> None:
        render = _module(self.repo_root / "infra/envs/target-secrets/render.py", "target_secrets_render")
        values = self.source_values()  # first: without the source there is nothing to hand over
        status = self._bao_status()
        init = self.vault.get("openbao-init")
        if not status.get("initialized"):
            if init is not None:
                raise DestinationError("openbao_init_material_without_server", "stored init response but server is new")
            out = self._bao(["bao", "operator", "init", "-key-shares=1", "-key-threshold=1", "-format=json"])
            # Keep the complete response first, then read fields (target-secrets README step 7).
            init = self.vault.put_once("openbao-init", json.loads(out))
        elif init is None:
            raise DestinationError("openbao_initialized_elsewhere", "the server is initialized and the platform has no recovery material")
        if self._bao_status().get("sealed"):
            self._bao(["sh", "-c", "exec bao write -format=json sys/unseal -"],
                      stdin=json.dumps({"key": init["unseal_keys_b64"][0]}))
            if self._bao_status().get("sealed"):
                raise DestinationError("openbao_still_sealed", "unseal did not take effect")
        token = init["root_token"]
        mounts = json.loads(self._bao_tok(token, "bao secrets list -format=json"))
        if "secret/" not in mounts:
            self._bao_tok(token, "bao secrets enable -path=secret -version=2 kv")
        self._bao_tok(token, "bao write secret/config -", body={"cas_required": True})
        auths = json.loads(self._bao_tok(token, "bao auth list -format=json"))
        if "kubernetes/" not in auths:
            self._bao_tok(token, "bao auth enable kubernetes")
        self._bao_tok(token, "bao write auth/kubernetes/config -", body={"kubernetes_host": "https://kubernetes.default.svc"})
        self._bao_tok(token, "bao policy write target-app-reader -", raw=render.policy())
        self._bao_tok(token, "bao write auth/kubernetes/role/target-app-reader -", body={
            "bound_service_account_names": "target-eso-reader", "bound_service_account_namespaces": APP_NS,
            "audience": "openbao", "token_policies": "target-app-reader", "token_ttl": "300s",
            "token_max_ttl": "900s", "token_no_default_policy": True})
        accessor = json.loads(self._bao_tok(token, "bao auth list -format=json"))["kubernetes/"]["accessor"]
        self._bao_tok(token, f"bao policy write {render.READER_POLICY} -", raw=render.rehearsal_reader_policy(accessor))
        self._bao_tok(token, f"bao policy write {render.WRITER_ACCOUNT} -", raw=render.rehearsal_writer_policy())
        self._bao_tok(token, f"bao write auth/kubernetes/role/{render.WRITER_ACCOUNT} -", body=render.rehearsal_writer_auth_role())
        objects = self.vault.get("seaweedfs-identities")["client"]
        admin = self.vault.put_once("target-app-admin", {"token": _random(40)})
        for path, data in [(render.DB_PATH, {"credentials_json": values["credentials_json"]}),
                           (render.OBJECT_PATH, {"access_key": objects["access_key"], "secret_key": objects["secret_key"]}),
                           (render.ADMIN_PATH, {"token": admin["token"]})]:
            self._kv_put_once(token, path, data)
        self.vault.put_once("done-openbao-init", {"source_version_id": values.get("source_version_id")})

    def app_metrics_client(self, run: dict) -> None:
        """docs/observability/certificates.md: one app CA per destination (90 days), a client leaf (30 days)."""
        ca = self.vault.get("app-metrics-ca") or self.vault.put_once("app-metrics-ca", _ca("bravo-app-metrics-ca", days=90))
        leaf = _leaf(ca, "demo-chat-target", days=30, client=True)
        self._create_secret(APP_NS, "bravo-app-metrics-client",
                            {"ca.crt": ca["ca_crt"], "tls.crt": leaf["crt"], "tls.key": leaf["key"]})

    # --- helpers -----------------------------------------------------------------------------------

    def _create_secret(self, namespace: str, name: str, data: dict, *, secret_type: str = "Opaque") -> None:
        if self.kube.get("secret", name, namespace) is not None:
            return  # created earlier by this action; never replaced
        body = {"apiVersion": "v1", "kind": "Secret", "type": secret_type,
                "metadata": {"name": name, "namespace": namespace,
                             "labels": {"app.kubernetes.io/managed-by": "bravo-migration"}},
                "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}}
        self.kube.run("create", "-f", "-", input=json.dumps(body))

    def _bao(self, argv: list[str], *, stdin: str | None = None) -> str:
        out = self.kube.exec(BAO_NS, BAO_POD, argv, container="openbao", input=stdin, timeout_s=60)
        return out.stdout

    def _bao_status(self) -> dict:
        out = self.kube.exec(BAO_NS, BAO_POD, ["bao", "status", "-format=json"], container="openbao", check=False)
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError as e:
            raise DestinationError("openbao_status_unreadable", "bao status gave no JSON") from e

    def _bao_tok(self, token: str, command: str, *, body: dict | None = None, raw: str | None = None) -> str:
        """Run one bao command with the token read from stdin (never argv) and its data after it."""
        data = raw if raw is not None else (json.dumps(body) if body is not None else "")
        script = f"IFS= read -r BAO_TOKEN; export BAO_TOKEN; exec {command}"
        try:
            return self._bao(["sh", "-c", script], stdin=token + "\n" + data)
        except KubeError as e:
            raise DestinationError("openbao_command_failed", f"{command.split(' -')[0]} failed") from e

    def _kv_put_once(self, token: str, path: str, data: dict) -> None:
        current = self.kube.exec(BAO_NS, BAO_POD, ["sh", "-c", "IFS= read -r BAO_TOKEN; export BAO_TOKEN; "
                                                   f"exec bao kv get -format=json -mount=secret {path}"],
                                 container="openbao", input=token + "\n", check=False)
        if current.returncode == 0:
            stored = json.loads(current.stdout)["data"]["data"]
            if stored != data:
                raise DestinationError("openbao_value_drift", f"{path} already holds other values")
            return
        self._bao_tok(token, f"bao kv put -mount=secret -cas=0 {path} -", body=data)


def _openbao_tls() -> dict:
    """CA (90 days) and leaf (30 days) for target-openbao, in memory (target-secrets README step 4)."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bravo target-openbao CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
          .not_valid_after(now + timedelta(days=90))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    names = ["target-openbao", f"target-openbao.{BAO_NS}", f"target-openbao.{BAO_NS}.svc",
             f"target-openbao.{BAO_NS}.svc.cluster.local", "localhost"]
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "target-openbao")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([*map(x509.DNSName, names),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    return {"ca_crt": ca.public_bytes(pem).decode(), "leaf_crt": leaf.public_bytes(pem).decode(),
            "leaf_key": key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
            "ca_key": ca_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}


def _ca(name: str, *, days: int) -> dict:
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name}-{now:%Y%m%d}-{secrets.token_hex(4)}")])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    return {"ca_crt": cert.public_bytes(pem).decode(),
            "ca_key": key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}


def _leaf(ca: dict, name: str, *, days: int, client: bool) -> dict:
    now = datetime.now(UTC)
    ca_cert = x509.load_pem_x509_certificate(ca["ca_crt"].encode())
    ca_key = serialization.load_pem_private_key(ca["ca_key"].encode(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    usage = ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    return {"crt": cert.public_bytes(pem).decode(),
            "key": key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}
