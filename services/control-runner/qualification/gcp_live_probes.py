"""A11 live run of the gcp control probes against a real GKE cluster and GCP project.

Operator tool, not a platform API. Every Google call goes through ``gcp_rest``:

* app / intruder / reader principals run their calls inside their own pod in the probe
  namespace (``PodExecTransport``), so Workload Identity tokens never leave the cluster;
* the anonymous IAM-01.1 client is ``UrllibTransport(None)`` from this host (no credentials).

Side effects (all inside the probe namespace): a canary env var on the app Deployment
(SEC-01.3, restored afterwards) and one canary RoleBinding create + delete (AUD-01.3).

Inputs come from a private JSON file (``--config``); results and raw evidence go to a private
directory (``--out``, created 0700). Nothing here prints tokens or secret values; identifiers
are printed only as test results, so publish the README table, not this output.

    python qualification/gcp_live_probes.py --config PRIVATE.json --out PRIVATE_DIR
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bravo_controls import Catalog, Runner, default_registry  # noqa: E402
from bravo_controls.app_isolation import ClientObjectIsolationReader  # noqa: E402
from bravo_controls.audit_probes import CanaryReceipt  # noqa: E402
from bravo_controls.aws_probes import ObjectTarget  # noqa: E402
from bravo_controls.gcp_probes import (  # noqa: E402
    CloudLoggingAuditSearch,
    CloudLoggingSecretLogSearch,
    GcpSecretEncryptionTarget,
    GcpSecretTarget,
)
from bravo_controls.gcp_rest import PodExecTransport, UrllibTransport, rest_client  # noqa: E402
from bravo_controls.runner import Environment, RunContext  # noqa: E402
from bravo_controls.secret_logs import LogCanaryReceipt  # noqa: E402

APP = "a11-app"
CONTAINER_LOGS = ("stdout", "stderr")
AUDIT_LOGS = ("cloudaudit.googleapis.com%2Factivity",)


def kubectl(*args: str, stdin: str | None = None) -> str:
    done = subprocess.run(
        ["kubectl", *args], input=stdin, check=True, capture_output=True, text=True, timeout=240
    )
    return done.stdout


def pod(namespace: str, app: str) -> str:
    """The single Running, Ready, not-terminating pod of ``app`` (waits out a rollout)."""
    for _ in range(40):
        items = json.loads(kubectl("get", "pods", "-n", namespace, "-l", f"app={app}", "-o", "json"))["items"]
        names = [
            i["metadata"]["name"]
            for i in items
            if i["status"].get("phase") == "Running"
            and not i["metadata"].get("deletionTimestamp")
            and all(c.get("ready") for c in i["status"].get("containerStatuses", []))
        ]
        if len(names) == 1 and len(items) == 1:
            return names[0]
        time.sleep(3)
    raise RuntimeError(f"expected one running pod for {app}")


class Live:
    def __init__(self, cfg: dict, out: Path, *, simulate_leak: bool = False):
        self.cfg, self.out, self.simulate_leak = cfg, out, simulate_leak
        self.project, self.number, self.ns = cfg["project_id"], cfg["project_number"], cfg["namespace"]
        pool = f"principal://iam.googleapis.com/projects/{self.number}/locations/global/workloadIdentityPools/{self.project}.svc.id.goog/subject/ns/{self.ns}/sa/"
        self.app_principal = f"bravo-a11-app@{self.project}.iam.gserviceaccount.com"
        self.other_principal = f"bravo-a11-intruder@{self.project}.iam.gserviceaccount.com"
        self.reader_principal = pool + "a11-reader"
        self.env = Environment("gcp-a11", "target", "gcp:gke/a11-probe", "gcp")
        self.evidence: list[dict] = []
        self.actor = cfg["kubectl_actor"]

    def sink(self, label: str):
        return lambda payload: self.evidence.append({"probe": label, "payload": json.loads(payload)})

    def client(self, principal: str, app: str):
        return rest_client(principal, self.project, PodExecTransport(self.ns, pod(self.ns, app), container="probe"))

    # --- SEC-01.3 canary ----------------------------------------------------------------
    def log_canary(self, context: RunContext) -> LogCanaryReceipt:
        secret, marker = "bravo_secret_" + secrets.token_hex(32), "bravo_log_" + secrets.token_hex(16)
        streams = tuple(f"projects/{self.project}/logs/{x}" for x in CONTAINER_LOGS)
        started = datetime.now(UTC)
        # configured: the canary is placed in the app's configuration (env) and rolled out.
        kubectl("set", "env", "-n", self.ns, f"deploy/{APP}", f"BRAVO_LOG_CANARY={secret}")
        kubectl("rollout", "status", "-n", self.ns, f"deploy/{APP}", "--timeout=180s")
        # exercised: the app process writes the non-secret marker to both container streams.
        name = pod(self.ns, APP)
        kubectl("exec", "-n", self.ns, name, "-c", "probe", "--", "sh", "-c",
                f"echo 'a11 exercised {marker}' > /proc/1/fd/1; echo 'a11 exercised {marker}' > /proc/1/fd/2")
        if self.simulate_leak:
            # Negative check of the search itself: the app "logs" its canary (expected FAIL).
            kubectl("exec", "-n", self.ns, name, "-c", "probe", "--", "sh", "-c",
                    'echo "a11 leak $BRAVO_LOG_CANARY" > /proc/1/fd/1')
        # restored: the canary leaves the configuration again.
        kubectl("set", "env", "-n", self.ns, f"deploy/{APP}", "BRAVO_LOG_CANARY-")
        kubectl("rollout", "status", "-n", self.ns, f"deploy/{APP}", "--timeout=180s")
        ended = datetime.now(UTC)
        search = self.secret_log_search()
        # logs_drained: the marker is visible in every stream through ended_at.
        settled = None
        for _ in range(30):
            probe_receipt = LogCanaryReceipt(
                context, search.source_binding, streams, self.cfg["log_configuration_sha256"],
                secret, marker, started, ended, datetime.now(UTC),
                frozenset({"configured", "exercised", "restored", "logs_drained"}),
            )
            window = search(probe_receipt)
            seen = {e.stream for e in window.events if marker in e.message}
            if seen == set(streams):
                settled = datetime.now(UTC)
                break
            time.sleep(15)
        if settled is None:
            settled = datetime.now(UTC)
            print("SEC-01.3 canary marker not visible in every stream before the wait budget")
        self.log_settled = settled
        return LogCanaryReceipt(
            context, search.source_binding, streams, self.cfg["log_configuration_sha256"],
            secret, marker, started, ended, settled,
            frozenset({"configured", "exercised", "restored", "logs_drained"}),
        )

    def secret_log_search(self):
        return CloudLoggingSecretLogSearch(
            self.client(self.reader_principal, "a11-reader"), project_id=self.project,
            reader_principal=self.reader_principal, log_ids=CONTAINER_LOGS,
            resource_filter=(
                f'resource.type="k8s_container" AND resource.labels.namespace_name="{self.ns}"'
                f' AND resource.labels.container_name="probe" AND labels."k8s-pod/app"="{APP}"'
            ),
        )

    # --- AUD-01.3 canary ----------------------------------------------------------------
    def rbac_canary(self, context: RunContext) -> CanaryReceipt:
        marker = "bravo_audit_" + secrets.token_hex(16)
        started = datetime.now(UTC)
        binding = {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
            "metadata": {"name": marker, "namespace": self.ns, "labels": {"component": "a11"}},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "view"},
            "subjects": [{"kind": "ServiceAccount", "name": "a11-reader", "namespace": self.ns}],
        }
        kubectl("create", "-f", "-", stdin=json.dumps(binding))
        kubectl("delete", "rolebinding", "-n", self.ns, marker, "--wait=true")
        completed = datetime.now(UTC)
        return CanaryReceipt(
            context, "AUD-01.3", marker, self.actor, "none", self.ns, "none",
            started, completed, started + timedelta(minutes=10), frozenset({"create", "delete"}),
        )

    def audit_search(self):
        return CloudLoggingAuditSearch(
            self.client(self.reader_principal, "a11-reader"), project_id=self.project,
            reader_principal=self.reader_principal, log_ids=AUDIT_LOGS,
            resource_filter=f'resource.type="k8s_cluster" AND resource.labels.cluster_name="{self.cfg["cluster"]}"',
        )

    # --- run ----------------------------------------------------------------------------
    def run(self) -> dict:
        catalog = Catalog(
            json.loads((ROOT / "docs/controls/profiles.json").read_text()),
            json.loads((ROOT / "docs/controls/control-result.schema.json").read_text()),
        )
        run_id = "a11-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        context = RunContext(run_id, APP, self.env)
        log_receipt = self.log_canary(context)
        rbac_receipt = self.rbac_canary(context)
        audit = self.audit_search()
        for _ in range(40):  # Admin Activity entries arrive with a delay.
            now = min(datetime.now(UTC), rbac_receipt.deadline)
            events = audit(rbac_receipt, now).events
            names = [json.loads(e.message).get("verb") for e in events]
            if {"create", "delete"} <= set(names):
                break
            if now >= rbac_receipt.deadline:
                break
            time.sleep(15)
        print(f"AUD-01.3 marker events seen: {sorted(set(n for n in names if n))}")

        c = self.cfg
        secret = GcpSecretTarget(APP, self.env, self.project, self.number, c["secret_id"], c["secret_version"])
        obj = ObjectTarget(APP, self.env, self.project, c["location"], c["owner_bucket"], c["object"],
                           c["owner_generation"], provider="gcs")
        other_obj = ObjectTarget("a11-intruder", self.env, self.project, c["location"], c["other_bucket"],
                                 c["object"], c["other_generation"], provider="gcs")
        app_client = self.client(self.app_principal, APP)
        other_client = self.client(self.other_principal, "a11-intruder")
        reader = self.client(self.reader_principal, "a11-reader")
        anonymous = rest_client("anonymous", self.project, UrllibTransport(None))
        log_streams = log_receipt.streams
        wiring = {
            "IAM-01.1": dict(target=obj, app_client=app_client, anonymous_client=anonymous,
                             app_principal_arn=self.app_principal, evidence_sink=self.sink("IAM-01.1")),
            "SEC-01.1": dict(target=secret, app_client=app_client, other_client=other_client,
                             app_principal=self.app_principal, other_principal=self.other_principal,
                             evidence_sink=self.sink("SEC-01.1")),
            "ENC-02.1": dict(target=GcpSecretEncryptionTarget(secret, (c["kms_key"],)), reader_client=reader,
                             reader_principal=self.reader_principal, evidence_sink=self.sink("ENC-02.1")),
            "SEC-01.3": dict(receipt_provider=lambda ctx, spec: log_receipt, search=self.secret_log_search(),
                             source_binding=f"gcp-logging:projects/{self.project}", streams=log_streams,
                             configuration_sha256=c["log_configuration_sha256"], evidence_sink=self.sink("SEC-01.3")),
            "AUD-01.3": dict(receipt_provider=lambda ctx, spec: rbac_receipt, search=audit,
                             source_binding=f"gcp-logging:projects/{self.project}",
                             streams=tuple(f"projects/{self.project}/logs/{x}" for x in AUDIT_LOGS),
                             evidence_sink=self.sink("AUD-01.3")),
        }
        ids = catalog.profile("full", "1")
        probes = default_registry().probes(self.env, [s.test_id for s in ids], wiring,
                                           evidence_sink=self.sink("UNSUPPORTED"))
        control_run = Runner(catalog, probes).run(
            profile_id="full", profile_version="1", app=APP, environment=self.env,
            registered_context=context,
        )
        catalog.validate_run(control_run)

        # ISO-01.1 object leg (the DB leg needs a CNPG cluster this run does not have).
        reader_iso = ClientObjectIsolationReader(
            obj, other_obj, owner_client=app_client, other_client=other_client,
            owner_principal=self.app_principal, other_principal=self.other_principal,
            read_registration=lambda ctx: reader_iso.binding,
        )
        leg = reader_iso(context, "a11-iso-object")
        iso = {"positive": leg.positive, "outcome": leg.outcome, "reason": leg.reason, "details": leg.details}
        return {"run": control_run, "iso_object_leg": iso}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--simulate-leak", action="store_true",
                   help="SEC-01.3 negative check: the app writes its canary to stdout (expect FAIL)")
    args = p.parse_args(argv)
    cfg = json.loads(args.config.read_text())
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    live = Live(cfg, args.out, simulate_leak=args.simulate_leak)
    result = live.run()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw = args.out / f"a11-gcp-run-{stamp}.json"
    raw.write_text(json.dumps({**result, "evidence": live.evidence}, indent=2, default=str))
    os.chmod(raw, 0o600)
    for row in result["run"]["results"]:
        print(f'{row["test_id"]:9} {row["verdict"]:8} {row.get("unknown_reason") or "-"}'
              f' observed={row.get("observed")} positive={row.get("positive_control")}')
    iso = result["iso_object_leg"]
    print(f'ISO-01.1 object leg: positive={iso["positive"]} outcome={iso["outcome"]} reason={iso["reason"]}')
    for item in live.evidence:
        payload = item["payload"]
        brief = {k: payload.get(k) for k in ("observed", "unknown_reason", "positive", "negative",
                                              "postpositive", "found_actions", "searched_events",
                                              "leaking_events", "positive_streams", "key_version_states")
                 if k in payload}
        print(item["probe"], json.dumps(brief, default=str))
    print(f"raw evidence: {raw.name} (private)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
