"""PROPOSE without an operator (T49): the workflow host runs the initial AI proposal itself.

The same parts as the operator tool (operations/initial-proposal/run.py), nothing new in the trust chain:

- T10 relay (`initial_proposal_relay.handle`: fields, claim, store) is called in this process, which already
  holds the database, instead of over `kubectl exec` from an operator's laptop;
- `JobInitialProposal` creates the one Job (deterministic name: a repeat is AlreadyExists, never a second
  model call) and opens its sealed receipt with the recipient key;
- the intent ledger and recipient key live under the host's state volume instead of an operator's disk;
- the Job is created with the host's own in-cluster ServiceAccount (create/get Jobs, read Pod logs in
  bravo-initial only), the broker and its subscription stay where they are (operations/subscription).

`start` and `collect` are the operator tool's own steps; the tool imports them from here.
A Job that is still running is a hold (`initial_proposal_running`), not an error, and is never resubmitted.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bravo_agent.product_execution_contracts import ExecutionPending, ExecutionUnknown

from . import initial_proposal_job as job
from .initial_proposal import InitialIntent
from .initial_proposal_relay import handle
from .store import EventError


def intent_path(state_dir, plan_id, version):
    """The one intent started for a plan version (the claim forbids recomputing it)."""
    return Path(state_dir) / "intents" / f"{plan_id}-{version}.json"


def start(base, *, relay, client, state_dir):
    """fields -> recipient key -> T10 claim -> one Job. Raises ExecutionPending while the Job runs."""
    path = intent_path(state_dir, base["plan_id"], base["version"])
    if path.exists():  # already claimed: never recompute, only read
        intent = InitialIntent.model_validate_json(path.read_text())
    else:
        fields = relay(base | {"op": "fields"})["fields"]
        intent = InitialIntent(**fields, recipient_public_key=client.recipient(fields["request_id"]))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(intent.model_dump_json())
    relay(base | {"op": "claim", "recipient_public_key": intent.recipient_public_key})
    client.submit(intent)  # idempotent per request id
    return intent


def collect(base, *, relay, client, state_dir):
    """Read the Job's sealed receipt; T10 rechecks it (schema, lint, run, source, paths) and stores it once."""
    intent = InitialIntent.model_validate_json(intent_path(state_dir, base["plan_id"], base["version"]).read_text())
    receipt = client.read(intent)
    stored = relay(base | {"op": "store", "envelope": receipt})["result"]
    return {"request_id": intent.request_id, "runtime_source_sha": stored["runtime_source_sha"],
            "proposal_digest": stored["proposal_digest"]}


class InClusterKubectl:
    """The four calls JobInitialProposal makes, sent to the API with the Pod's own ServiceAccount token.

    The host image has no kubectl, and this is all it needs: list and read Jobs, create one, read the
    last log line of its Pod, in bravo-initial only (the Role allows nothing else). Same answers as
    kubectl: (exit code, stdout, stderr) with "AlreadyExists" for a repeated create. A transport error
    is an unconfirmed response, never a guess: the caller reads the same execution again.
    """

    TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

    def __init__(self, *, server="https://kubernetes.default.svc", namespace=job.NAMESPACE, send=None):
        self.server, self.namespace = server, namespace
        self.send = send or self._send

    def _send(self, method, path, body=None):
        import ssl
        import urllib.error
        import urllib.request

        token = Path(self.TOKEN).read_text().strip()  # projected and rotated: read per call
        request = urllib.request.Request(self.server + path, method=method, data=body, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {})})
        try:
            with urllib.request.urlopen(request, timeout=30, context=ssl.create_default_context(cafile=self.CA)) as r:
                return r.status, r.read(job.MAX_LOG + 1)
        except urllib.error.HTTPError as error:
            return error.code, error.read(4096)

    def __call__(self, args, payload=None):
        from urllib.parse import quote

        jobs = f"/apis/batch/v1/namespaces/{self.namespace}/jobs"
        try:
            if args[:3] == ["get", "jobs", "-l"]:
                status, out = self.send("GET", f"{jobs}?labelSelector={quote(args[3], safe='')}")
            elif args[:3] == ["create", "-f", "-"]:
                status, out = self.send("POST", jobs, payload)
                if status == 409:
                    return 1, b"", b"Error from server (AlreadyExists)"
                out = ("job.batch/" + json.loads(out)["metadata"]["name"]).encode() if status < 300 else out
            elif args[:2] == ["get", "job"]:
                status, out = self.send("GET", f"{jobs}/{quote(args[2], safe='')}")
            elif args[0] == "logs" and args[1].startswith("job/") and args[2:4] == ["--tail", "1"]:
                name = args[1].removeprefix("job/")
                status, out = self.send("GET", f"/api/v1/namespaces/{self.namespace}/pods?labelSelector="
                                                + quote(f"job-name={name}", safe=""))
                if status < 300:
                    pods = sorted(json.loads(out).get("items", []),
                                  key=lambda p: p["metadata"].get("creationTimestamp", ""))
                    if not pods:
                        return 1, b"", b"no pod for job"
                    status, out = self.send("GET", f"/api/v1/namespaces/{self.namespace}/pods/"
                                                   f"{quote(pods[-1]['metadata']['name'])}/log?tailLines=1")
            else:
                raise ValueError("unsupported_kubernetes_call")
        except ValueError:
            raise
        except Exception:
            raise ExecutionUnknown("KUBERNETES_RESPONSE_UNCONFIRMED") from None
        return (0, out, b"") if status < 300 else (1, b"", out[:4096])


class AutoInitialProposal:
    """One call per sweep: start if needed, then collect when the Job is done."""

    def __init__(self, records, *, relay_config, state_dir, image, source_revision, principal, kubectl=None,
                 catalog_root=None, admission_ttl=timedelta(hours=1), clock=None):
        self.records, self.config, self.principal = records, relay_config, principal
        self.catalog_root = Path(catalog_root) if catalog_root else None
        self.admission_ttl, self.clock = admission_ttl, clock or (lambda: datetime.now(UTC))
        self.state_dir = Path(state_dir)
        self.client = job.JobInitialProposal(self.state_dir, image=image, source_revision=source_revision,
                                             kubectl=kubectl or InClusterKubectl())

    def admit(self, plan_id, tenant_id, version):
        """Admit the plan's input for the Job identity when exactly one task is registered for the plan
        (rehearsal_runner TaskCatalog: <root>/<plan_id>/<task_id>.json). None or several is a hold:
        choosing among tasks is S1's decision, not this host's."""
        tasks = sorted(p.stem for p in (self.catalog_root / plan_id).glob("*.json")) if self.catalog_root else []
        if len(tasks) != 1:
            raise EventError("initial_task_unselected")
        expires_at = (self.clock() + self.admission_ttl).isoformat()
        return self.records.admit(self.principal, plan_id=plan_id, tenant_id=tenant_id, task_id=tasks[0],
                                  expected_version=version, expires_at=expires_at)

    def relay(self, request):
        return handle(self.records, request, self.config)

    def __call__(self, plan_id, version, ref):
        base = {"plan_id": plan_id, "version": version, "ref": ref, "principal": self.principal,
                "runtime_source_sha": self.client.source_revision}
        try:
            start(base, relay=self.relay, client=self.client, state_dir=self.state_dir)
            return collect(base, relay=self.relay, client=self.client, state_dir=self.state_dir)
        except ExecutionPending:
            raise EventError("initial_proposal_running") from None
