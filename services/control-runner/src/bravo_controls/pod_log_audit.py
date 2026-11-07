"""T34 audit search of a CNPG instance's own log through the Kubernetes API → AuditBatch.

Neither environment forwards CNPG logs to a searchable store yet (ADR-0016's Object Lock store and
CloudWatch shipping are not deployed), so AUD-01.1 reads the PostgreSQL log where it is written: the
primary instance container's log (`pods/log`, kubelet timestamps). This is the original source, not a
tamper-evident archive (ADR-0037 records the limits). It fails closed:

- the container must have been running since before the canary started, and the pod UID, restart
  count and start time must be the same before and after the read; otherwise lines may be missing and
  the batch is incomplete (UNKNOWN, never absent);
- a read that reaches the byte limit is incomplete;
- only lines inside the canary window that contain the marker are returned, like the CloudWatch and
  object-store searches.

`read(path) -> bytes` is a GET on the cluster API (the operator's kubectl, never a token here).
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from .audit_probes import MAX_BYTES, MAX_EVENTS, AuditBatch, AuditEvent, CanaryReceipt, _utc

NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?\Z")
LINE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) (.*)\Z")


def _instant(value: str) -> datetime:
    # kubelet writes RFC 3339 with nanoseconds; Python keeps microseconds.
    head, _, rest = value.removesuffix("Z").partition(".")
    return datetime.fromisoformat(head + ("." + rest[:6] if rest else "") + "+00:00")


def pod_path(namespace: str, pod: str) -> str:
    return f"/api/v1/namespaces/{namespace}/pods/{pod}"


class PodLogAuditSearch:
    """One registered pod and container; reads only (pods, pods/log)."""

    def __init__(self, read, *, namespace: str, pod: str, container: str, source_binding: str,
                 clock=lambda: datetime.now(UTC)):
        if (not callable(read) or not callable(clock)
                or any(not isinstance(v, str) or not NAME.fullmatch(v) for v in (namespace, pod, container))
                or not isinstance(source_binding, str) or not source_binding):
            raise ValueError("registered pod log source required")
        self.read, self.clock = read, clock
        self.namespace, self.pod, self.container = namespace, pod, container
        self.source_binding = source_binding
        self.stream = f"{namespace}/{pod}/{container}"

    def _state(self):
        pod = json.loads(self.read(pod_path(self.namespace, self.pod)))
        status = next((s for s in pod.get("status", {}).get("containerStatuses", [])
                       if s.get("name") == self.container), None)
        running = (status or {}).get("state", {}).get("running") or {}
        if not running.get("startedAt"):
            return None
        return pod["metadata"]["uid"], status.get("restartCount"), _instant(running["startedAt"])

    def __call__(self, receipt: CanaryReceipt, through: datetime) -> AuditBatch:
        through = _utc(through)
        if not isinstance(receipt, CanaryReceipt) or not receipt.started_at <= through <= receipt.deadline:
            raise ValueError("search interval outside authorized canary window")
        before = self._state()
        since = (receipt.started_at - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = self.read(f"{pod_path(self.namespace, self.pod)}/log?container={quote(self.container)}"
                        f"&timestamps=true&sinceTime={quote(since)}&limitBytes={MAX_BYTES}")
        after = self._state()
        queried = _utc(self.clock())
        complete = (before is not None and before == after and before[2] <= receipt.started_at
                    and len(raw) < MAX_BYTES)
        events = []
        for index, line in enumerate(raw.decode("utf-8", errors="replace").splitlines()):
            if receipt.marker not in line:
                continue
            match = LINE.fullmatch(line)
            if not match:
                complete = False  # a line we cannot place in time
                continue
            at = _instant(match[1])
            if not receipt.started_at <= at <= through:
                continue
            events.append(AuditEvent(hashlib.sha256(f"{index}\0{line}".encode()).hexdigest()[:32],
                                     self.stream, at, at, match[2]))
        if len(events) > MAX_EVENTS:
            raise ValueError("excessive audit events")
        return AuditBatch(receipt.context, receipt.marker, self.source_binding, (self.stream,),
                          receipt.started_at, through, queried, tuple(events), complete)
