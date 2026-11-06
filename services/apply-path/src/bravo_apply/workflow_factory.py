"""T31 `application` factory for the T10 workflow host (`ports.json` "application").

    "application": "bravo_apply.workflow_factory:application"

`application(store)` returns `{"REHEARSE": preparer, "GATE": preparer}` over one ApplicationHost
for this process (workflow_ports.create_ports). Its own protected JSON, next to ports.json in the
same ConfigMap (`/workflow/apply.json`, or `BRAVO_T31_APPLY_CONFIG`), names mounted files only:

    {"version": 1,
     "kubeconfig_file": "/var/run/secrets/bravo-t31-target/kubeconfig",
     "receipts_dir": "/var/lib/bravo/t31/applications",
     "target": {"environment_id": "<registered target>", "namespace": "demo-chat",
                "deployment": "demo-chat", "container": "app",
                "repository": "ghcr.io/softbank-hackathon-finals/demo-chat"},
     "host_subject": "workflow-host",
     "trusted_ports": "module:factory"}

`trusted_ports(store)` is reviewed deployment code owned by T10/T15/T19. It returns
`{"candidate": fn(c, principal, plan, claim) -> (proposal, diff_bytes), "check_current": fn,
"current_change": fn (optional, default ChangeIntake.current), "gate": {"records", "prepare",
"service_factory", "load_gate"} (optional; absent leaves GATE unwired)}`. The target, the cluster
identity and the receipt volume come only from this file, never from those ports.

Fail closed. The kubeconfig must be a mounted file with one https cluster and a user credential;
kubectl must be on PATH; the receipt directory's parent must be a mounted directory. Otherwise the
preparers raise a named hold (`t31_target_kubeconfig_unavailable`, `t31_kubectl_unavailable`,
`t31_receipts_mount_unavailable`) and no KubectlKube is made, so the plan stays in REHEARSE. The
check is repeated each sweep, so a Secret mounted later takes effect without a restart. There is no
fallback to the Pod's in-cluster identity (the chart mounts no token; KubectlKube also drops
KUBERNETES_SERVICE_HOST/PORT). A malformed apply.json or trusted port fails startup instead.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .deploy import KubectlKube

log = logging.getLogger(__name__)
CONFIG_ENV = "BRAVO_T31_APPLY_CONFIG"
CONFIG_FILE = "/workflow/apply.json"
FACTORY = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")
NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
REPOSITORY = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?(?:/[a-z0-9._-]+)+")
FIELDS = {"version", "kubeconfig_file", "receipts_dir", "target", "host_subject", "trusted_ports"}
TARGET = {"environment_id", "namespace", "deployment", "container", "repository"}
MAX_KUBECONFIG = 64 * 1024
USER_CREDENTIALS = ("token", "tokenFile", "client-certificate-data", "client-certificate", "exec")


class TargetUnavailable(RuntimeError):
    """A named reason the target cannot be reached from this process. Never carries file content."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class ApplyHostConfig:
    kubeconfig_file: Path
    receipts_dir: Path
    environment_id: str
    namespace: str
    deployment: str
    container: str
    repository: str
    host_subject: str
    trusted_ports: str


def load_config(path):
    """Protected apply.json. Any deviation is a deployment error (startup fails)."""
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise ValueError("t31_apply_configuration_unavailable") from None
    target = value.get("target") if type(value) is dict else None
    if (type(value) is not dict or set(value) != FIELDS or value["version"] != 1
            or type(target) is not dict or set(target) != TARGET):
        raise ValueError("t31_apply_configuration_invalid")
    paths = [value["kubeconfig_file"], value["receipts_dir"]]
    if any(not isinstance(p, str) or not Path(p).is_absolute() or ".." in Path(p).parts for p in paths):
        raise ValueError("t31_apply_paths_invalid")
    if (not all(isinstance(target[k], str) and NAME.fullmatch(target[k]) for k in ("namespace", "deployment", "container"))
            or not isinstance(target["environment_id"], str) or not IDENTIFIER.fullmatch(target["environment_id"])
            or not isinstance(target["repository"], str) or not REPOSITORY.fullmatch(target["repository"])
            or not isinstance(value["host_subject"], str) or not IDENTIFIER.fullmatch(value["host_subject"])
            or not isinstance(value["trusted_ports"], str) or not FACTORY.fullmatch(value["trusted_ports"])):
        raise ValueError("t31_apply_target_invalid")
    return ApplyHostConfig(Path(paths[0]), Path(paths[1]), target["environment_id"], target["namespace"],
                           target["deployment"], target["container"], target["repository"],
                           value["host_subject"], value["trusted_ports"])


def _parse(text):
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        import yaml  # the registration image has PyYAML; JSON kubeconfigs need nothing
    except ImportError:
        raise TargetUnavailable("t31_target_kubeconfig_unavailable", "unparseable") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        raise TargetUnavailable("t31_target_kubeconfig_unavailable", "unparseable") from None


def _named(items, name, key):
    found = [i.get(key) for i in items or [] if isinstance(i, dict) and i.get("name") == name]
    return found[0] if len(found) == 1 and isinstance(found[0], dict) else None


def check_kubeconfig(path, *, namespace):
    """Structure only: the current context resolves to one https cluster and a user credential.

    An empty or partial kubeconfig is exactly the case where client-go would use in-cluster
    credentials, so it is refused here before kubectl ever runs. Secret volumes mount the file
    through a symlink, which is allowed; the target must still be a regular file.
    """
    unavailable = "t31_target_kubeconfig_unavailable"
    path = Path(path)
    try:
        if not path.is_file():
            raise TargetUnavailable(unavailable, "missing")
        if path.stat().st_size > MAX_KUBECONFIG:
            raise TargetUnavailable(unavailable, "too_large")
        text = path.read_text()
    except OSError:
        raise TargetUnavailable(unavailable, "unreadable") from None
    if not text.strip():
        raise TargetUnavailable(unavailable, "empty")
    config = _parse(text)
    if not isinstance(config, dict) or not config.get("current-context"):
        raise TargetUnavailable(unavailable, "no_current_context")
    context = _named(config.get("contexts"), config["current-context"], "context")
    cluster = None if context is None else _named(config.get("clusters"), context.get("cluster"), "cluster")
    user = None if context is None else _named(config.get("users"), context.get("user"), "user")
    if cluster is None or user is None:
        raise TargetUnavailable(unavailable, "context_unresolved")
    if not str(cluster.get("server", "")).startswith("https://") or cluster.get("insecure-skip-tls-verify"):
        raise TargetUnavailable(unavailable, "cluster_not_verified_https")
    if not any(user.get(k) for k in USER_CREDENTIALS) or user.get("auth-provider"):
        raise TargetUnavailable(unavailable, "user_credential_missing")
    if user.get("exec") and not shutil.which(str(user["exec"].get("command", ""))):
        raise TargetUnavailable(unavailable, "exec_command_missing")
    if context.get("namespace") not in (None, namespace):
        raise TargetUnavailable(unavailable, "namespace_mismatch")


def target_kube(config: ApplyHostConfig) -> KubectlKube:
    check_kubeconfig(config.kubeconfig_file, namespace=config.namespace)
    if shutil.which("kubectl") is None:
        raise TargetUnavailable("t31_kubectl_unavailable")
    return KubectlKube(str(config.kubeconfig_file))


def receipts(config: ApplyHostConfig) -> Path:
    """The leaf directory may be created; its parent (the PVC mount) must already exist."""
    root, parent = config.receipts_dir, config.receipts_dir.parent
    if not parent.is_dir() or parent.is_symlink() or root.is_symlink():
        raise TargetUnavailable("t31_receipts_mount_unavailable")
    try:
        root.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        raise TargetUnavailable("t31_receipts_mount_unavailable") from None
    return root


def _factory(spec):
    module, name = spec.split(":")
    return getattr(importlib.import_module(module), name)


def _trusted(value):
    allowed = {"candidate", "check_current", "current_change", "gate"}
    if (type(value) is not dict or set(value) - allowed or {"candidate", "check_current"} - set(value)
            or not all(callable(value[k]) for k in ("candidate", "check_current"))
            or ("current_change" in value and not callable(value["current_change"]))):
        raise ValueError("t31_trusted_ports_invalid")
    gate = value.get("gate")
    if gate is not None and (type(gate) is not dict or set(gate) != {"records", "prepare", "service_factory", "load_gate"}
                             or not callable(gate["load_gate"])):
        raise ValueError("t31_trusted_ports_invalid")
    return value


def application(store, config_file=None):
    from bravo_platform_events.application_host import ApplicationHost, ApplyInput, GateRecheck
    from bravo_platform_events.change import ChangeApprovalService
    from bravo_platform_events.change_intake import ChangeIntake
    from bravo_platform_events.operator_access import OperatorAccess
    from bravo_platform_events.store import EventError
    from bravo_platform_events.workflow_stages import application_stages

    config = load_config(config_file or os.environ.get(CONFIG_ENV, CONFIG_FILE))
    ports = _trusted(_factory(config.trusted_ports)(store))
    access = OperatorAccess(store)
    # Same S2 composition as create_operator_cutover_app: current human grants over the T15 intake.
    changes = ChangeApprovalService(store, authority=access.change_authority(ports.get("current_change", ChangeIntake.current)),
                                    check_access=access.check_read)
    gate = ports.get("gate")
    recheck = gate and GateRecheck(gate["records"], prepare=gate["prepare"], service_factory=gate["service_factory"])

    def load_input(c, principal, plan, claim):
        proposal, diff = ports["candidate"](c, principal, plan, claim)
        return ApplyInput(proposal, diff, config.environment_id, config.namespace, config.deployment,
                          config.container, config.repository)

    def gate_unwired(*_):
        raise EventError("workflow_reader_unwired")

    built = {}

    def stages():
        """Build once the target is reachable; until then every call is the same named hold."""
        if "stages" not in built:
            try:
                kube, root = target_kube(config), receipts(config)
                host = ApplicationHost(changes, kube=kube, receipts_dir=root, load_input=load_input)
            except TargetUnavailable as error:
                if built.get("reason") != (error.code, error.detail):
                    log.warning(json.dumps({"event": "t31.application.hold", "reason": error.code,
                                            "detail": error.detail}, sort_keys=True))
                    built["reason"] = (error.code, error.detail)
                raise EventError(error.code) from None
            except BlockingIOError:  # another process holds the receipt lock
                raise EventError("t31_application_host_locked") from None
            built["stages"] = application_stages(
                host, recheck, receipts_dir=root, check_current=ports["check_current"],
                load_gate=gate["load_gate"] if gate else gate_unwired, host_subject=config.host_subject)
            log.info(json.dumps({"event": "t31.application.ready", "namespace": config.namespace,
                                 "deployment": config.deployment, "gate": bool(gate)}, sort_keys=True))
        return built["stages"]

    def preparer(state):
        def prepare(plan_id, tenant_id, version):
            wired = stages()
            if state not in wired:
                raise EventError("workflow_reader_unwired")
            return wired[state](plan_id, tenant_id, version)
        return prepare

    return {"REHEARSE": preparer("REHEARSE"), "GATE": preparer("GATE")}
