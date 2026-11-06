"""Initial proposal as one platform Kubernetes Job instead of a GitHub Actions run.

The operator keeps the same private ledger and recipient key, then creates one Job with
a deterministic name: a repeated create is AlreadyExists, never a second model call.
The Job reaches the loopback broker on the platform node with its projected token and
prints only the receipt sealed to the operator's key. Model keys never enter the Job.
"""

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from bravo_agent.contracts import digest
from bravo_agent.product_execution_contracts import ExecutionPending, ExecutionUnknown
from bravo_agent.subscription_product import stopped
from bravo_agent.subscription_product_seal import seal_bound_receipt, validate_recipient

from .initial_proposal import InitialIntent
from .initial_proposal_execution import InitialTunnelCall
from .initial_proposal_runtime import InitialBrokerRequest
from .initial_proposal_transport import (
    OperatorInitialProposal,
    initial_receipt_binding,
    validate_initial_receipt,
)
from .product_api import JOB_PREFIX

NAMESPACE = "bravo-initial"
SERVICE_ACCOUNT = "initial-proposal"
AUDIENCE = "bravo-subscription-broker"
TOKEN = Path("/var/run/secrets/bravo-initial/token")
BROKER = "https://127.0.0.1:18761/run"
MAX_LOG = 2 * 1024 * 1024


def execution_id(request_id):
    """Numeric, stable per logical request; kept in the existing github_run_id fields."""
    # ponytail: wire fields keep their Actions names; rename with the next contract version.
    return str(int(digest({"initial-job": request_id})[:15], 16) or 1)


def job_name(request_id):
    return JOB_PREFIX + execution_id(request_id)


def job_manifest(intent, image):
    if re.fullmatch(r"[a-z0-9./-]+@sha256:[a-f0-9]{64}", image) is None:
        raise ValueError("immutable job image required")
    run_id = execution_id(intent.request_id)
    env = [{"name": "PRODUCT_INTENT", "value": json.dumps(intent.model_dump(mode="json"), sort_keys=True)},
           {"name": "PRODUCT_EXECUTION_ID", "value": run_id},
           {"name": "PRODUCT_BROKER_CA_PEM", "valueFrom": {"configMapKeyRef": {
               "name": "subscription-broker-ca", "key": "ca.pem"}}}]
    return {"apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": job_name(intent.request_id), "namespace": NAMESPACE,
                     "labels": {"app.kubernetes.io/name": "bravo-initial-proposal"},
                     "annotations": {"bravo.dev/request-digest": digest(intent.request_id)}},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 600, "ttlSecondsAfterFinished": 86400,
            "template": {"metadata": {"labels": {"app.kubernetes.io/name": "bravo-initial-proposal"}},
                "spec": {"restartPolicy": "Never", "serviceAccountName": SERVICE_ACCOUNT,
                    "automountServiceAccountToken": False, "enableServiceLinks": False,
                    # The broker listens on node loopback only, like the SSM tunnel endpoint.
                    "hostNetwork": True, "dnsPolicy": "ClusterFirstWithHostNet",
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{"name": "initial", "image": image,
                        "command": ["python", "-m", "bravo_platform_events.initial_proposal_job", "run"],
                        "env": env,
                        "resources": {"requests": {"cpu": "50m", "memory": "128Mi"},
                                      "limits": {"memory": "256Mi"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "token", "mountPath": str(TOKEN.parent), "readOnly": True}]}],
                    "volumes": [{"name": "token", "projected": {"sources": [{"serviceAccountToken": {
                        "audience": AUDIENCE, "expirationSeconds": 600, "path": TOKEN.name}}]}}]}}}}


def run(env=os.environ, *, call=None, token_file=TOKEN):
    intent = InitialIntent.model_validate_json(env["PRODUCT_INTENT"])
    run_id = env["PRODUCT_EXECUTION_ID"]
    if run_id != execution_id(intent.request_id):
        raise ValueError("execution id mismatch")
    validate_recipient(intent.recipient_public_key)
    binding = initial_receipt_binding(intent, run_id)
    # No tunnel or window to close on this path; the operator stops the broker afterwards.
    receipt = binding | {"status": 502, "cleanup_confirmed": True, "result": stopped("INITIAL_EXECUTION_UNCONFIRMED")}
    try:
        body = InitialBrokerRequest(intent=intent, github_run_id=run_id,
                                    web_identity_token=token_file.read_text().strip())
        status, result = (call or InitialTunnelCall(env["PRODUCT_BROKER_CA_PEM"], url=BROKER))(body.model_dump())
        receipt = receipt | {"status": status, "result": result}
        validate_initial_receipt(intent, run_id, receipt)
    except Exception:
        pass
    return receipt, seal_bound_receipt(intent.recipient_public_key, binding, receipt)


class OperatorKubectl:
    """The operator's existing kubeconfig; no cluster credential is installed anywhere."""

    def __init__(self, context=None):
        self.prefix = ["kubectl"] + (["--context", context] if context else []) + ["-n", NAMESPACE]

    def __call__(self, args, payload=None):
        try:
            result = subprocess.run([*self.prefix, *args], input=payload, capture_output=True,
                                    timeout=60, check=False)
        except Exception:
            raise ExecutionUnknown("KUBERNETES_RESPONSE_UNCONFIRMED") from None
        return result.returncode, result.stdout[:MAX_LOG + 1], result.stderr[:4096]


class JobInitialProposal(OperatorInitialProposal):
    """Same ledger, recipient key and receipt checks as Actions; Kubernetes as the carrier."""

    def __init__(self, state_dir, *, image, kubectl=None):
        super().__init__(state_dir, github=object())  # Never reaches GitHub on this path.
        self.image = image
        self.kubectl = kubectl or OperatorKubectl()

    def submit(self, intent):
        intent = self._parse_intent(intent)
        manifest = job_manifest(intent, self.image)
        with self._db() as db:
            exists = db.execute("SELECT 1 FROM executions WHERE request_id=?", (intent.request_id,)).fetchone()
        if exists:
            return self.read(intent)
        if self._private_key(intent.request_id).public_key().public_bytes_raw().hex() != intent.recipient_public_key:
            raise ExecutionUnknown("EXECUTION_RECIPIENT_CONFLICT")
        metadata = {"carrier": "platform-job", "job": manifest["metadata"]["name"], "namespace": NAMESPACE,
                    "image": self.image, "dispatch_sent_at": datetime.now(UTC).isoformat()}
        run_id = execution_id(intent.request_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            fresh = db.execute("INSERT OR IGNORE INTO executions VALUES (?,?,?,?)",
                (intent.request_id, json.dumps(intent.model_dump()), json.dumps(metadata), run_id)).rowcount == 1
        if fresh:
            # The committed row survives a lost response; the fixed name makes create idempotent.
            code, _, err = self.kubectl(["create", "-f", "-", "-o", "name"], json.dumps(manifest).encode())
            if code and b"AlreadyExists" not in err:
                raise ExecutionUnknown("JOB_CREATE_UNCONFIRMED_READ_SAME_EXECUTION")
        return self.read(intent)

    def read(self, intent):
        intent = self._parse_intent(intent)
        metadata, run_id = self._record(intent)
        if metadata.get("carrier") != "platform-job" or run_id != execution_id(intent.request_id):
            raise ExecutionUnknown("EXECUTION_CARRIER_CONFLICT")
        private = self._private_key(intent.request_id)
        if private.public_key().public_bytes_raw().hex() != intent.recipient_public_key:
            raise ExecutionUnknown("EXECUTION_RECIPIENT_CONFLICT")
        code, out, _ = self.kubectl(["get", "job", metadata["job"], "-o", "json"])
        if code:
            raise ExecutionUnknown("JOB_NOT_OBSERVED")
        job = json.loads(out)
        if (job["metadata"].get("annotations", {}).get("bravo.dev/request-digest") != digest(intent.request_id)
                or job["spec"]["template"]["spec"]["containers"][0]["image"] != metadata["image"]
                or job["spec"].get("backoffLimit") != 0):
            raise ExecutionUnknown("JOB_BINDING_CONFLICT")
        status = job.get("status", {})
        if not status.get("succeeded") and not status.get("failed"):
            raise ExecutionPending("JOB_IN_PROGRESS")
        if status.get("succeeded") != 1:
            raise ExecutionUnknown("JOB_DID_NOT_COMPLETE_SUCCESSFULLY")
        code, out, _ = self.kubectl(["logs", "job/" + metadata["job"], "--tail", "1"])
        if code or len(out) > MAX_LOG:
            raise ExecutionUnknown("JOB_RECEIPT_UNAVAILABLE")
        try:
            return self._validate_receipt(intent, run_id, self._open_receipt(intent, run_id, json.loads(out), private))
        except Exception:
            raise ExecutionUnknown("JOB_RECEIPT_INVALID") from None


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["run", "submit", "read"])
    parser.add_argument("--intent", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--image")
    parser.add_argument("--context")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.operation == "run":
        try:
            _, sealed = run()
        except Exception:
            print("Initial execution unconfirmed; read this same execution only.", file=sys.stderr)
            return 1
        # Sealed: only the operator's key opens it. A non-200 receipt is still evidence, so
        # the Job succeeds whenever it is printed and the operator's receipt check decides.
        print(json.dumps(sealed, allow_nan=False))
        return 0
    try:
        if not (args.intent and args.state_dir and args.image and args.output):
            raise ValueError("intent, state dir, image and output required")
        client = JobInitialProposal(args.state_dir, image=args.image, kubectl=OperatorKubectl(args.context))
        receipt = getattr(client, args.operation)(InitialIntent.model_validate_json(args.intent.read_bytes()))
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(receipt, stream, allow_nan=False)
        print(json.dumps({"state": "COMPLETE", "execution_id": receipt["github_run_id"],
                          "receipt_digest": digest(receipt)}))
        return 0
    except ExecutionPending as error:
        print(json.dumps({"state": "PENDING", "reason": str(error)}))
        return 4
    except Exception:
        print(json.dumps({"state": "UNKNOWN", "reason": "READ_SAME_EXECUTION_NO_RECREATE"}))
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
