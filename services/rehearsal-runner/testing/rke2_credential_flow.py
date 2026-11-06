"""Operator-run: the T15 credential-migration path on a prepared RKE2 target.

Real code: T10 RehearsalService · ChangeIntake · ChangeApprovalService (SQLite), T11 Coordinator
and tool policy, T3 isolation, the credential verification Jobs (writer Job, rehearsal Postgres,
candidate app with T9's init container, probes, cleanup Job), UID cleanup and signed attempt
records. Fixtures: a scripted proposal read from a file instead of a model, a throwaway workload
JWT key and the operator principal. This is not a T38 subscription run or a human approval.

Prerequisites on the target (not created here): T9 OpenBao with the `rehearsal-app-reader`
template policy, writer auth role and 8200 ingress; `bravo-rehearsal-control` with
`rehearsal-openbao-writer`; the runner identity's RBAC; a candidate image built from exactly the
proposal's diff (`diff` prints its hash).

Optional `runner_kubeconfig` (the `t15-runner` short-lived token): Jobs and probes then run as the
runner, and namespace/T3 provisioning stays on the KUBECONFIG management client through T7's
`ScopedPlanner` (#398), the same split as the deployed host. Without it everything uses KUBECONFIG.

Run from services/platform-events with KUBECONFIG whose current context is the target:
  PYTHONPATH=../rehearsal-runner/src:../agent-worker/src uv run --frozen \
    --no-default-groups --with pydantic --with pyjwt --with cryptography \
    python ../rehearsal-runner/testing/rke2_credential_flow.py diff|run --config <private json>
"""

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

from rehearsal_runner.credential_candidate import file_diff
from rehearsal_runner.credential_proposer import SOURCE_PATHS

CONFIG_KEYS = {"state", "cluster_uid", "source_dir", "source_sha", "proposal", "candidate_image",
               "bao_image", "postgres_image", "probe_image", "isolation_image", "openbao_ca_file",
               "t9_render", "profile"}
OPTIONAL_KEYS = {"runner_kubeconfig", "probe_from_source"}
# The modules Dockerfile.credential-probe installs; mounted read-only when no probe image exists.
PROBE_MODULES = ("__init__.py", "credential_probe.py", "kube_secret_probe.py", "app_health_probe.py",
                 "credential_job_probe.py")
RUNNER = "system:serviceaccount:bravo-rehearsal-control:t15-runner"
SCOPED = pathlib.Path(__file__).resolve().parents[3] / "infra/envs/rehearsal-access/scoped_lifecycle.py"


def load_config(path: pathlib.Path) -> dict:
    config = json.loads(path.read_text())
    if not CONFIG_KEYS <= set(config) <= CONFIG_KEYS | OPTIONAL_KEYS:
        raise SystemExit(f"config keys must be {sorted(CONFIG_KEYS)} plus optional {sorted(OPTIONAL_KEYS)}")
    return config


def proposal_diff(config: dict) -> tuple[bytes, list[dict]]:
    """The diff T7 must build the candidate from; edits come from the scripted proposal file."""
    source = pathlib.Path(config["source_dir"])
    edits = json.loads(pathlib.Path(config["proposal"]).read_text())["edits"]
    if not edits or any(e["path"] not in SOURCE_PATHS for e in edits):
        raise SystemExit("proposal edits must stay within the credential source paths")
    diff = b"".join(file_diff(e["path"], (source / e["path"]).read_text(), e["replacement"]) for e in edits)
    return diff, edits


def require_cluster(uid: str) -> None:
    out = subprocess.run(["kubectl", "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"],
                         capture_output=True, text=True, timeout=30)
    if out.returncode or out.stdout.strip() != uid:
        raise SystemExit("current context is not the configured target cluster")


def runner_clients(path: str):
    """T38 Kube and isolation Kubectl that read the runner's own kubeconfig, after checking its identity."""
    from bravo_agent.verification import Kube

    from rehearsal_runner.isolation.kube import Kubectl

    who = subprocess.run(["kubectl", "--kubeconfig", path, "auth", "whoami", "-o",
                          "jsonpath={.status.userInfo.username}"], capture_output=True, text=True, timeout=30)
    if who.returncode or who.stdout.strip() != RUNNER:
        raise SystemExit("runner kubeconfig does not authenticate as t15-runner")

    class RunnerKube(Kube):
        def call(self, *args, data=None):
            return super().call("--kubeconfig", path, *args, data=data)

    class RunnerKubectl(Kubectl):
        def run(self, *args, stdin=None, timeout=60):
            return super().run("--kubeconfig", path, *args, stdin=stdin, timeout=timeout)

    return RunnerKube(), RunnerKubectl()


def scoped(planner, provisioner, cleaner):
    """Wrap with T7's ScopedPlanner (#398), loaded from the repository like the host does."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("t7_scoped_lifecycle", SCOPED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ScopedPlanner(planner, provisioner=provisioner, cleaner=cleaner)


def run(config: dict) -> int:
    import jwt
    from bravo_agent.contracts import FailureBundle, SnapshotManifest
    from bravo_agent.coordinator import Coordinator
    from bravo_agent.tools import Snapshot
    from bravo_agent.verification import Kube
    from bravo_platform_events.change import ChangeApprovalService
    from bravo_platform_events.change_intake import Candidate, ChangeIntake
    from bravo_platform_events.plan_api import Principal
    from bravo_platform_events.plans import PlanSpec, PlanStore
    from bravo_platform_events.rehearsal import RehearsalService
    from bravo_platform_events.workload_identity import WorkloadSender
    from cryptography.hazmat.primitives.asymmetric import rsa

    from rehearsal_runner.approval_handoff import submit_candidate
    from rehearsal_runner.attempt_store import AttemptStore, PlanBinding
    from rehearsal_runner.bucket_planner import build_bucket_host
    from rehearsal_runner.credential_job import CredentialFixture
    from rehearsal_runner.credential_planner import CredentialPlanner
    from rehearsal_runner.credential_proposer import credential_summary
    from rehearsal_runner.isolation.kube import Kubectl
    from rehearsal_runner.isolation.profile import read_env
    from rehearsal_runner.secret_task import prepare_credential_migration
    from rehearsal_runner.t9_delivery import load_t9_delivery

    require_cluster(config["cluster_uid"])
    state = pathlib.Path(config["state"])
    diff, edits = proposal_diff(config)
    expected = "sha256:" + hashlib.sha256(diff).hexdigest()
    sha, source = config["source_sha"], pathlib.Path(config["source_dir"])
    work = pathlib.Path(tempfile.mkdtemp(prefix="t15-cred-", dir=state))
    snapshot_root = work / "snapshot"
    for path in SOURCE_PATHS:
        (snapshot_root / path).parent.mkdir(parents=True, exist_ok=True)
        (snapshot_root / path).write_bytes((source / path).read_bytes())
    files = {p: hashlib.sha256((snapshot_root / p).read_bytes()).hexdigest() for p in SOURCE_PATHS}
    manifest = SnapshotManifest(source_sha=sha, files=files, editable_paths=list(SOURCE_PATHS))
    task = prepare_credential_migration("catalog", sha)
    spec = PlanSpec("plan-cred-rke2", "tenant-local", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
    store = PlanStore(work / "t10.db")
    store.register_plan(spec, run_id="local", occurred_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    with store._connection() as connection:  # T10's state machine owns this transition in a deployment.
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (spec.plan_id,))
    records = AttemptStore(work / "records", os.urandom(32))

    class Provider:
        """Scripted proposal: read the sources through T11 and submit the configured edits."""

        async def propose(self, bundle, tools):
            reads = {p: tools.dispatch("mcp__bravo__repo_read", {"path": p}) for p in SOURCE_PATHS}
            tools.dispatch("mcp__bravo__plan_propose", {
                "run_id": bundle.run_id, "source_sha": bundle.source_sha, "risk": "medium",
                "rationale": "Read the injected credential file instead of calling Secrets Manager",
                "edits": [{"path": e["path"], "before_sha256": reads[e["path"]]["sha256"],
                           "replacement": e["replacement"]} for e in edits]})

    class LocalProposals:
        def open(self, admission, bound_task):
            bundle = FailureBundle(run_id=bound_task.run_id, source_sha=sha, failure_code="CODE_CHANGE_REQUIRED",
                                   evidence_digest="0" * 64, summary=credential_summary(bound_task))
            return Coordinator(bundle, Snapshot(snapshot_root, manifest)), Provider()

    def candidate_image(diff_hash: str) -> str:
        # Stand-in for the T7 publication reader: only the image built from this exact diff.
        return config["candidate_image"] if diff_hash == expected else ""

    src = pathlib.Path(__file__).resolve().parents[1] / "src/rehearsal_runner"
    probe_sources = (tuple((m, (src / m).read_text()) for m in PROBE_MODULES)
                     if config.get("probe_from_source") is True else ())
    fixture = CredentialFixture(bao_image=config["bao_image"], postgres_image=config["postgres_image"],
                                probe_image=config["probe_image"], probe_sources=probe_sources,
                                openbao_ca_pem=pathlib.Path(config["openbao_ca_file"]).read_text())
    admin, management = Kube(), Kubectl()
    runner_path = config.get("runner_kubeconfig")
    job_kube, job_kubectl = runner_clients(runner_path) if runner_path else (admin, management)
    planner = CredentialPlanner(
        profile=read_env(pathlib.Path(config["profile"])), proposals=LocalProposals(),
        bindings=lambda _a: PlanBinding(spec.plan_digest, spec.target_environment_id), kubectl=job_kubectl,
        kube=job_kube, isolation_image=config["isolation_image"], fixture=fixture,
        delivery=load_t9_delivery(pathlib.Path(config["t9_render"])), candidate_image=candidate_image,
        cleaner=admin)
    if runner_path:
        planner = scoped(planner, provisioner=management, cleaner=admin)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    workload = WorkloadSender("https://issuer.invalid", "t10-local", "t15-runner", key.public_key(), spec.tenant_id)

    def token():
        now = int(time.time())
        return jwt.encode({"iss": "https://issuer.invalid", "aud": "t10-local", "sub": "t15-runner",
                           "iat": now, "exp": now + 300}, key, algorithm="RS256")

    deliveries, intake_box = [], []
    host, evidence = build_bucket_host(Catalog(task), planner, records, deliver=lambda s: deliveries.append(
        submit_candidate(s, candidate_type=Candidate, intake=intake_box[0], token_supplier=token).state))
    intake = ChangeIntake(store, sender=workload, read_original=evidence.read_original,
                          authorize_hold=lambda _c, who, *_: who == "local-security-fixture")
    intake_box.append(intake)
    service = RehearsalService(store, host=host, check_start=lambda *_: None)
    operator = Principal("local-operator", spec.tenant_id)
    started = time.monotonic()
    receipt = service.start(operator, spec.plan_id, task_id="credentials", expected_version=1,
                            attempt=1, command_key="start-1")
    result = service.reconcile(operator, spec.plan_id, rehearsal_id=receipt["rehearsal_id"], attempt=1)
    record = records.read(receipt["rehearsal_id"], 1) or {}
    leftovers = json.loads(subprocess.run(["kubectl", "get", "namespaces", "-o", "json"], capture_output=True,
                                          text=True, timeout=30).stdout)["items"]
    report = {"environment": "operator-rke2", "model_provider": "scripted_fixture", "source_sha": sha,
              "job_identity": RUNNER if runner_path else "operator-kubeconfig",
              "probe": "runner_sources_on_pinned_python" if probe_sources else "probe_image",
              "expected_diff_hash": expected, "seconds": round(time.monotonic() - started),
              "t10_status": result["status"], "rehearsal_id": receipt["rehearsal_id"], "deliveries": deliveries,
              "record": {k: record.get(k) for k in ("verdict", "reason", "detail", "verification_id", "namespace",
                                                     "created_uid", "removed_uid", "isolation_probe_run_id",
                                                     "diff_hash", "image_digest", "evidence")},
              # Only this run's namespaces: its rehearsal namespace (run label) and T3 comparison namespace.
              # Other rehearsal-* namespaces (e.g. a teammate's test space) are not this run's leftovers.
              "leftover_namespaces": [n["metadata"]["name"] for n in leftovers if mine(n, receipt["rehearsal_id"], record)],
              "other_rehearsal_namespaces": [n["metadata"]["name"] for n in leftovers
                                             if n["metadata"]["name"].startswith("rehearsal-")
                                             and not mine(n, receipt["rehearsal_id"], record)]}
    if result["status"] == "PASS":
        with store._connection() as connection:
            current = intake.current(connection, operator, {"plan_id": spec.plan_id, "version": 1},
                                     rehearsal_id=receipt["rehearsal_id"])
        report["awaiting_approval_binding"] = current.binding
        approval = ChangeApprovalService(store, authority=intake.current).approve(
            operator, spec.plan_id, run_id=receipt["rehearsal_id"], binding=current.binding,
            expected_version=1, command_key="approve-local")
        report["fixture_operator_approval"] = {k: approval[k] for k in ("approval_kind", "run_id", "binding")}
    report["success"] = result["status"] == "PASS" and not report["leftover_namespaces"]
    out = state / f"t15-credential-flow-{time.strftime('%Y%m%dT%H%M%S')}.json"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
    print(json.dumps({"success": report["success"], "status": result["status"], "report": str(out),
                      "reason": record.get("reason"), "detail": record.get("detail"), "seconds": report["seconds"]}))
    return 0 if report["success"] else 1


def mine(namespace: dict, rehearsal_id: str, record: dict) -> bool:
    """Namespace created by this run: the attempt's run label, or its T3 comparison namespace."""
    from rehearsal_runner.recovery_lookup import recovery_identity

    labels = namespace["metadata"].get("labels", {})
    own, _ = recovery_identity(rehearsal_id, 1)
    probe = record.get("isolation_probe_run_id")
    return (all(labels.get(k) == v for k, v in own.items())
            or (probe is not None and namespace["metadata"]["name"] == f"isolation-victim-{probe}"))


class Catalog:
    def __init__(self, task):
        self.task = task

    def for_plan(self, connection, plan, task_id):
        if task_id != "credentials":
            raise ValueError("unknown task")
        return self.task

    def for_admission(self, admission):
        return self.task


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["diff", "run"])
    parser.add_argument("--config", required=True, type=pathlib.Path, help="private JSON, see CONFIG_KEYS")
    args = parser.parse_args()
    os.umask(0o077)
    cfg = load_config(args.config)
    if args.command == "diff":
        body, _ = proposal_diff(cfg)
        print(json.dumps({"diff_hash": "sha256:" + hashlib.sha256(body).hexdigest(), "bytes": len(body)}))
        sys.exit(0)
    sys.exit(run(cfg))
