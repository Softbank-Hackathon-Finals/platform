"""inspect and probe actions in space A, and the tool Pod they probe from.

Every API call impersonates the namespace's `rehearsal-exec` account, so the
Role in templates/20-rbac.yaml bounds it (the T3 isolation test checks that the
impersonation takes effect). The namespace comes from the session, never from
the model. The probe runs a platform-owned script in the tokenless tool Pod
with the arguments as argv; no shell parses model input.
"""

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .exec_contract import PROBE_OUTCOMES, inspect_record, probe_record
from .exec_session import (
    ActionDenied,
    ActionFailed,
    ActionTimeout,
    NotSent,
    Outcome,
    Space,
    Transient,
)
from .recovery_lookup import recovery_identity

TOOL_POD = "rehearsal-exec-tool"
TOOL_ROLE_LABEL = "bravo.platform/role"
_IMAGE = re.compile(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}")
_COMMAND_RAN = "command terminated with exit code"


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: bytes
    stderr: str


Runner = Callable[[list[str], float], Result]


@dataclass(frozen=True)
class KubectlRunner:
    """Bytes-preserving kubectl; output is screened before anyone decodes it as text."""

    context: str | None = None

    def __call__(self, args: list[str], timeout: float) -> Result:
        cmd = ["kubectl"] + (["--context", self.context] if self.context else []) + args
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=max(timeout, 0.1))
        except subprocess.TimeoutExpired:
            raise ActionTimeout() from None
        return Result(out.returncode, out.stdout, out.stderr.decode("utf-8", "replace"))


def _as(space: Space) -> list[str]:
    return ["--as", f"system:serviceaccount:{space.name}:rehearsal-exec", "-n", space.name]


def _remaining(deadline: float, clock: Callable[[], float]) -> float:
    left = deadline - clock()
    if left <= 0:
        raise NotSent()  # nothing was sent yet
    return left


def _classify(result: Result) -> None:
    """Raise for an API-level failure; return when the call reached the API and succeeded."""
    if result.returncode == 0:
        return
    text = result.stderr
    if "Forbidden" in text or "forbidden" in text:
        raise ActionDenied()
    if any(s in text for s in ("Unable to connect", "connection refused", "no such host")):
        raise NotSent()
    raise Transient()


def _not_found(result: Result) -> bool:
    return result.returncode != 0 and ("NotFound" in result.stderr or "not found" in result.stderr)


def _status(found: bool, **values) -> dict:
    base = {"kind": "inspect", "found": found, "phase": None, "ready": None, "replicas": None,
            "ready_replicas": None, "restarts": None, "events": None}
    return base | values


@dataclass(frozen=True)
class Inspector:
    space: Space
    kubectl: Runner = field(default_factory=KubectlRunner, repr=False)
    log_container: str | None = None  # the template's application container
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def validate(self, arguments: dict) -> dict:
        return inspect_record(arguments)

    def _call(self, args: list[str], deadline: float) -> Result:
        return self.kubectl(_as(self.space) + args, _remaining(deadline, self.clock))

    def run(self, arguments: dict, deadline: float) -> Outcome:
        kind, name, lines = arguments["target_kind"], arguments["target_name"], arguments["lines"]
        if kind == "logs":
            args = ["logs", "pod/" + name, f"--tail={lines}"]
            if self.log_container:
                args += ["-c", self.log_container]
            result = self._call(args, deadline)
            if _not_found(result):
                return Outcome(_status(False))
            _classify(result)
            return Outcome(_status(True), result.stdout)
        if kind == "events":
            args = ["get", "events", "-o", "json"]
            if name is not None:
                args += ["--field-selector", "involvedObject.name=" + name]
            result = self._call(args, deadline)
            _classify(result)
            items = _items(result.stdout)
            text = "".join(_event_line(e) for e in items).encode("utf-8")
            return Outcome(_status(True, events=len(items)), text)
        result = self._call(["get", kind, name, "-o", "json"], deadline)
        if _not_found(result):
            return Outcome(_status(False))
        _classify(result)
        obj = _object(result.stdout)
        status = obj.get("status") or {}
        if kind == "pod":
            conditions = {c.get("type"): c.get("status") for c in status.get("conditions") or []}
            restarts = sum(int(c.get("restartCount") or 0) for c in status.get("containerStatuses") or [])
            phase = status.get("phase") if status.get("phase") in (
                "Pending", "Running", "Succeeded", "Failed", "Unknown") else "Unknown"
            return Outcome(_status(True, phase=phase, ready=conditions.get("Ready") == "True",
                                   restarts=restarts))
        replicas = int((obj.get("spec") or {}).get("replicas") or 0)
        ready = int(status.get("readyReplicas") or 0)
        return Outcome(_status(True, ready=ready == replicas, replicas=replicas, ready_replicas=ready))


def _object(data: bytes) -> dict:
    try:
        value = json.loads(data)
    except ValueError:
        raise Transient() from None
    if not isinstance(value, dict):
        raise Transient()
    return value


def _items(data: bytes) -> list[dict]:
    items = _object(data).get("items")
    if not isinstance(items, list):
        raise Transient()
    return sorted((i for i in items if isinstance(i, dict)),
                  key=lambda e: str(e.get("lastTimestamp") or e.get("eventTime") or ""))


def _event_line(event: dict) -> str:
    target = event.get("involvedObject") or {}
    return (f"{event.get('type', '')} {event.get('reason', '')} "
            f"{target.get('kind', '')}/{target.get('name', '')}: {event.get('message', '')}\n")


# Runs inside the tool Pod. argv: host port method path timeout. Prints one JSON
# line, then at most 64 KiB of the HTTP body. Always exits 0 once it ran.
PROBE_SCRIPT = r'''
import http.client, json, socket, sys, time
host, port, method, path, timeout = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], float(sys.argv[5])
start, status, body = time.monotonic(), None, b""
def outcome(error):
    if isinstance(error, socket.gaierror): return "DNS_FAILED"
    if isinstance(error, (socket.timeout, TimeoutError)): return "TIMED_OUT"
    return "REFUSED"
try:
    if method == "TCP":
        socket.create_connection((host, port), timeout=timeout).close()
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET" if method == "HTTP_GET" else "HEAD", path)
        response = conn.getresponse()
        status = response.status
        body = response.read(65536) if method == "HTTP_GET" else b""
        conn.close()
    result = "CONNECTED"
except Exception as error:
    result = outcome(error)
elapsed = min(15000, int((time.monotonic() - start) * 1000))
sys.stdout.buffer.write(json.dumps({"probe_outcome": result, "http_status": status, "elapsed_ms": elapsed}).encode() + b"\n" + body)
'''


@dataclass(frozen=True)
class Prober:
    space: Space
    kubectl: Runner = field(default_factory=KubectlRunner, repr=False)
    pod: str = TOOL_POD
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def validate(self, arguments: dict) -> dict:
        return probe_record(arguments)

    def run(self, arguments: dict, deadline: float) -> Outcome:
        left = _remaining(deadline, self.clock)
        connect = max(0.5, min(10.0, left - 2))
        host = f"{arguments['service']}.{self.space.name}.svc"
        result = self.kubectl(_as(self.space) + [
            "exec", self.pod, "-c", "tool", "--", "python3", "-I", "-c", PROBE_SCRIPT,
            host, str(arguments["port"]), arguments["method"], arguments["path"] or "/", str(connect)], left)
        if result.returncode != 0:
            if _COMMAND_RAN in result.stderr:
                raise ActionFailed(result.stdout)
            if "Forbidden" in result.stderr or "forbidden" in result.stderr:
                raise ActionDenied()
            # Pod missing, container not running or API unreachable: the probe never ran.
            raise NotSent()
        head, _, body = result.stdout.partition(b"\n")
        try:
            observed = json.loads(head)
        except ValueError:
            raise ActionFailed(result.stdout) from None
        http_status = observed.get("http_status") if isinstance(observed, dict) else None
        if (not isinstance(observed, dict) or observed.get("probe_outcome") not in PROBE_OUTCOMES
                or not (http_status is None or (type(http_status) is int and 100 <= http_status <= 599))
                or type(observed.get("elapsed_ms")) is not int or not 0 <= observed["elapsed_ms"] <= 15000):
            raise ActionFailed(result.stdout)
        if arguments["method"] == "TCP":
            http_status = None
        status = {"kind": "probe", "probe_outcome": observed["probe_outcome"], "http_status": http_status,
                  "elapsed_ms": observed["elapsed_ms"]}
        record = {"probe_outcome": observed["probe_outcome"]} | (
            {"http_status": http_status} if http_status is not None else {})
        return Outcome(status, body, record)


def tool_pod_manifest(space: Space, image: str, run_id: str, attempt: int) -> dict:
    """Tokenless tool Pod from the runner template; the model cannot change any field."""
    if _IMAGE.fullmatch(image) is None:
        raise ValueError("tool image must be pinned by digest")
    labels, annotations = recovery_identity(run_id, attempt)
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": TOOL_POD, "namespace": space.name,
                     "labels": labels | {TOOL_ROLE_LABEL: "exec-tool"}, "annotations": annotations},
        "spec": {
            "serviceAccountName": "default", "automountServiceAccountToken": False,
            "enableServiceLinks": False, "restartPolicy": "Never", "activeDeadlineSeconds": 600,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "tool", "image": image, "imagePullPolicy": "IfNotPresent",
                "command": ["python3", "-I", "-c", "import time; time.sleep(600)"],
                "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                              "limits": {"cpu": "200m", "memory": "128Mi"}},
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
            }],
        },
    }
