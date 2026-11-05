"""Local-only: run the T15 bucket path on the personal RKE2 test VM.

Real code: T10 RehearsalService · ChangeIntake · ChangeApprovalService (SQLite),
T11 Coordinator and tool policy, T3 isolation, the SeaweedFS policy Jobs, UID
cleanup and the signed attempt records. Fixtures: a scripted proposal instead
of a model, a synthetic source revision, local SeaweedFS and OpenBao stand-ins,
a throwaway workload JWT key and the operator principal. This is not team
environment evidence, a T38 subscription run or a human approval.

Run from services/platform-events with KUBECONFIG pointing at the VM:
  PYTHONPATH=../rehearsal-runner/src:../agent-worker/src uv run --frozen \
    --no-default-groups --with pydantic python ../rehearsal-runner/testing/rke2_rehearsal_flow.py \
    setup|run --state <private dir>
"""

import argparse
import base64
import hashlib
import json
import os
import pathlib
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from string import Template

FIXTURE_NS, OPENBAO_NS = "t15-fixture-objects", "bravo-secrets"
SEAWEED_IMAGE = ("docker.io/chrislusf/seaweedfs:4.52@sha256:"
                 "a54ba35b44895a449f5cdd415999062201b17b9835ec0ffc19ea605b72ffa3d1")
AWS_IMAGE = ("docker.io/amazon/aws-cli@sha256:"
             "bc6b7bba44ce38f9604ede49c584824af919047ea03fbcc7c7610671fdef95d8")
BUSYBOX = "busybox:1.36"
BUCKET, CANARY = "rehearsal-proof", b"t15 local bucket policy test\n"
SOURCE_PATH, TARGET_PATH = "src/source-policy.json", "src/target-policy.json"
VM_NODE = "bravo-t15-rke2"


def kubectl(*args, data=None, check=True, timeout=120):
    proc = subprocess.run(["kubectl", *args], input=json.dumps(data) if data is not None else None,
                          capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode:
        raise RuntimeError("kubectl_failed:" + args[0])
    return proc


def get(*args):
    return json.loads(kubectl("get", *args, "-o", "json").stdout)


def require_vm():
    nodes = get("nodes")["items"]
    if [n["metadata"]["name"] for n in nodes] != [VM_NODE]:
        raise RuntimeError("not_the_local_test_vm")


def policy(effect):
    """public: the S3 source. overbroad: passes T11 but also denies signed reads.

    T11 rejects any translation that grants Allow to "*", so a wrong proposal
    that reaches the cluster must look like this one.
    """
    resource = Template("arn:aws:s3:::${bucket}/*").substitute(bucket=BUCKET)
    if effect == "public":
        statement = {"Sid": "PublicRead", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
                     "Resource": resource}
    elif effect == "overbroad":
        statement = {"Sid": "DenyAllRead", "Effect": "Deny", "Principal": "*", "Action": "s3:GetObject",
                     "Resource": resource}
    else:
        statement = {"Sid": "DenyAnonymousRead", "Effect": "Deny", "Principal": "*",
                     "Action": "s3:GetObject", "Resource": resource,
                     "Condition": {"StringEquals": {"aws:PrincipalType": "Anonymous"}}}
    return json.dumps({"Version": "2012-10-17", "Statement": [statement]}, sort_keys=True)


def wait_ready(namespace, pod, seconds=900):
    kubectl("-n", namespace, "wait", "--for=condition=Ready", "pod/" + pod, f"--timeout={seconds}s",
            timeout=seconds + 30)


def ensure(obj):
    """Create once; a rerun of setup keeps existing fixture objects and their values."""
    meta = obj["metadata"]
    scope = ["-n", meta["namespace"]] if "namespace" in meta else []
    if kubectl("get", obj["kind"].lower(), meta["name"], *scope, "--ignore-not-found", "-o", "name").stdout.strip():
        return
    kubectl("create", "-f", "-", data=obj)


def setup(state: pathlib.Path):
    """Create the dedicated test bucket and an OpenBao stand-in; values stay in the cluster."""
    require_vm()
    security = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    pod_security = {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                    "seccompProfile": {"type": "RuntimeDefault"}}
    for name, labels in ((FIXTURE_NS, {"purpose": "t15-bucket-policy-test"}),
                         (OPENBAO_NS, {"purpose": "t15-openbao-standin"})):
        ensure({"apiVersion": "v1", "kind": "Namespace", "metadata": {
            "name": name, "labels": labels | {"pod-security.kubernetes.io/enforce": "restricted"}}})
    access, secret = "T15" + secrets.token_hex(9).upper(), secrets.token_hex(24)
    config = {"identities": [{"name": "t15-test-admin", "credentials": [{"accessKey": access, "secretKey": secret}],
                              "actions": ["Admin", "Read", "Write", "List"]}]}
    ensure({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "t15-s3-config", "namespace": FIXTURE_NS},
            "data": {"s3.json": base64.b64encode(json.dumps(config).encode()).decode(),
                     "access": base64.b64encode(access.encode()).decode(),
                     "secret": base64.b64encode(secret.encode()).decode()}})
    ensure({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "deny-ingress", "namespace": FIXTURE_NS},
            "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [
                {"from": [{"podSelector": {"matchLabels": {"role": "bootstrap"}}}],
                 "ports": [{"protocol": "TCP", "port": 8333}]}]}})
    ensure({"apiVersion": "v1", "kind": "Pod", "metadata": {
        "name": "seaweed", "namespace": FIXTURE_NS, "labels": {"app": "t15-bucket-policy-test"}},
        "spec": {"automountServiceAccountToken": False, "securityContext": pod_security,
                 "volumes": [{"name": "data", "emptyDir": {"sizeLimit": "1Gi"}},
                             {"name": "auth", "secret": {"secretName": "t15-s3-config", "defaultMode": 288,
                                                         "items": [{"key": "s3.json", "path": "s3.json"}]}}],
                 "containers": [{"name": "seaweed", "image": SEAWEED_IMAGE,
                                 "args": ["server", "-s3", "-dir=/data", "-ip=$(POD_IP)", "-ip.bind=0.0.0.0",
                                          "-s3.config=/etc/t15/s3.json", "-master.volumeSizeLimitMB=128",
                                          "-volume.max=10"],  # a new bucket collection grows several volumes
                                 "env": [{"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}],
                                 "ports": [{"containerPort": 8333}], "securityContext": security,
                                 "resources": {"limits": {"memory": "512Mi"}},
                                 "volumeMounts": [{"name": "data", "mountPath": "/data"},
                                                  {"name": "auth", "mountPath": "/etc/t15", "readOnly": True}],
                                 "readinessProbe": {"tcpSocket": {"port": 8333}, "periodSeconds": 5}}]}})
    ensure({"apiVersion": "v1", "kind": "Pod", "metadata": {
        "name": "openbao-standin", "namespace": OPENBAO_NS,
        "labels": {"app.kubernetes.io/instance": "target-openbao"}},
        "spec": {"automountServiceAccountToken": False, "securityContext": pod_security,
                 "containers": [{"name": "listen", "image": BUSYBOX, "command": ["httpd", "-f", "-p", "8200"],
                                 "ports": [{"containerPort": 8200}],
                                 "securityContext": security | {"readOnlyRootFilesystem": True}}]}})
    wait_ready(FIXTURE_NS, "seaweed")
    wait_ready(OPENBAO_NS, "openbao-standin")
    ip = get("pod", "seaweed", "-n", FIXTURE_NS)["status"]["podIP"]
    script = ("set -eu; printf 't15 local bucket policy test\\n' > /tmp/canary; "
              'aws --endpoint-url "$E" s3api head-bucket --bucket "$B" 2>/dev/null || '
              'aws --endpoint-url "$E" s3api create-bucket --bucket "$B" >/dev/null; '
              'aws --endpoint-url "$E" s3api put-object --bucket "$B" --key canary --body /tmp/canary >/dev/null; '
              'echo ready')
    env = [{"name": "E", "value": f"http://{ip}:8333"}, {"name": "B", "value": BUCKET},
           {"name": "HOME", "value": "/tmp"}, {"name": "AWS_DEFAULT_REGION", "value": "us-east-1"},
           {"name": "AWS_EC2_METADATA_DISABLED", "value": "true"}]
    env += [{"name": k, "valueFrom": {"secretKeyRef": {"name": "t15-s3-config", "key": v}}}
            for k, v in (("AWS_ACCESS_KEY_ID", "access"), ("AWS_SECRET_ACCESS_KEY", "secret"))]
    ensure({"apiVersion": "batch/v1", "kind": "Job", "metadata": {
        "name": "bootstrap", "namespace": FIXTURE_NS}, "spec": {"backoffLimit": 2, "template": {
            "metadata": {"labels": {"role": "bootstrap"}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                     "securityContext": pod_security | {"runAsUser": 65532, "runAsGroup": 65532},
                     "volumes": [{"name": "tmp", "emptyDir": {}}],
                     "containers": [{"name": "run", "image": AWS_IMAGE, "command": ["/bin/sh", "-c", script],
                                     "env": env, "securityContext": security | {"readOnlyRootFilesystem": True},
                                     "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}]}}}})
    kubectl("-n", FIXTURE_NS, "wait", "--for=condition=complete", "job/bootstrap", "--timeout=900s", timeout=930)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    print(json.dumps({"fixture": "ready", "namespace": FIXTURE_NS, "bucket": BUCKET}))


class PatientCleaner:
    """Local VM only: keep confirming absence of the same UID past T38's 60 s window."""

    def __init__(self, inner, seconds=600):
        self.inner, self.seconds = inner, seconds

    def cleanup(self, namespace, uid):
        if self.inner.cleanup(namespace, uid):
            return True
        deadline = time.monotonic() + self.seconds
        while time.monotonic() < deadline:
            raw = kubectl("get", "namespace", namespace, "--ignore-not-found", "-o", "json").stdout
            if not raw.strip():
                return True
            if json.loads(raw)["metadata"]["uid"] != uid:
                return False
            time.sleep(5)
        return False


class AccessRule:
    """Fixture-only: open the test SeaweedFS to one rehearsal namespace, removed by UID."""

    def __init__(self, inner):
        self.inner, self.attempt, self.rule = inner, inner.attempt, None

    def create(self, run_id):
        namespace = self.inner.create(run_id)
        try:
            created = json.loads(kubectl("create", "-f", "-", "-o", "json", data={
                "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": "allow-" + namespace.name, "namespace": FIXTURE_NS},
                "spec": {"podSelector": {"matchLabels": {"app": "t15-bucket-policy-test"}},
                         "policyTypes": ["Ingress"], "ingress": [{"from": [{"namespaceSelector": {
                             "matchLabels": {"kubernetes.io/metadata.name": namespace.name}}}],
                             "ports": [{"protocol": "TCP", "port": 8333}]}]}}).stdout)
            self.rule = (created["metadata"]["name"], created["metadata"]["uid"])
        except Exception:
            self.inner.cleanup(namespace)
            raise
        return namespace

    def cleanup(self, namespace):
        removed = self.rule is None
        try:
            if self.rule:
                name, uid = self.rule
                kubectl("delete", "--raw", f"/apis/networking.k8s.io/v1/namespaces/{FIXTURE_NS}/networkpolicies/{name}",
                        "-f", "-", data={"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}})
                removed = not kubectl("get", "networkpolicy", name, "-n", FIXTURE_NS, "--ignore-not-found",
                                      "-o", "name").stdout.strip()
        finally:
            cleaned = self.inner.cleanup(namespace)
        return cleaned and removed


def run(state: pathlib.Path, first: str):
    import jwt
    from bravo_agent.contracts import FailureBundle, SnapshotManifest, VerificationReceipt
    from bravo_agent.coordinator import Coordinator
    from bravo_agent.tools import PolicyDenied, Snapshot
    from bravo_agent.verification import Kube
    from bravo_platform_events.change import ChangeApprovalService
    from bravo_platform_events.change_intake import Candidate, ChangeIntake
    from bravo_platform_events.plan_api import Principal
    from bravo_platform_events.plans import PlanSpec, PlanStore
    from bravo_platform_events.rehearsal import RehearsalService
    from bravo_platform_events.workload_identity import WorkloadSender
    from cryptography.hazmat.primitives.asymmetric import rsa

    from rehearsal_runner.approval_handoff import submit_candidate
    from rehearsal_runner.attempt_store import (
        AttemptStore,
        PlanBinding,
        RecordedAttemptSender,
        RecordedEvidence,
    )
    from rehearsal_runner.bucket_job import BucketFixture, SeaweedBucketJob
    from rehearsal_runner.bucket_proposal_source import BoundBucketProposalSource
    from rehearsal_runner.bucket_proposer import T11BucketProposer, bucket_summary
    from rehearsal_runner.bucket_task import prepare_bucket_policy_task
    from rehearsal_runner.bucket_verifier import BucketVerifier
    from rehearsal_runner.isolation.kube import Kubectl
    from rehearsal_runner.isolation.profile import targets
    from rehearsal_runner.namespace_lifecycle import NamespaceLifecycle
    from rehearsal_runner.rehearsal_host import T15RehearsalHost
    from rehearsal_runner.retry import AttemptPorts
    from rehearsal_runner.runner import T3Gate

    require_vm()
    seaweed = get("pod", "seaweed", "-n", FIXTURE_NS)
    auth = get("secret", "t15-s3-config", "-n", FIXTURE_NS)["data"]
    credential = (base64.b64decode(auth["access"]).decode(), base64.b64decode(auth["secret"]).decode())
    fixture = BucketFixture(f"http://{seaweed['status']['podIP']}:8333", BUCKET, "canary",
                            hashlib.sha256(CANARY).hexdigest(), AWS_IMAGE)
    profile = {"SEAWEEDFS_NAMESPACE": FIXTURE_NS, "SEAWEEDFS_LABEL_KEY": "app",
               "SEAWEEDFS_LABEL_VALUE": "t15-bucket-policy-test", "SEAWEEDFS_PORT": "8333",
               "OPENBAO_NAMESPACE": OPENBAO_NS, "OPENBAO_LABEL_KEY": "app.kubernetes.io/instance",
               "OPENBAO_LABEL_VALUE": "target-openbao", "OPENBAO_PORT": "8200"}
    stamp = time.strftime("%Y%m%dT%H%M%S")
    report = {"environment": "local-rke2-qemu", "cleanup_wait_seconds": {"t3_fixtures": 300, "namespace": 660}, "job_deadline_seconds": 900, "model_provider": "scripted_fixture",
              "source_revision": "synthetic_fixture", "openbao": "standin", "first_proposal": first,
              "fixture_seaweedfs_image": SEAWEED_IMAGE, "attempts": []}
    sha = "1" * 40
    source = policy("public")
    work = pathlib.Path(tempfile.mkdtemp(prefix="t15-flow-", dir=state))
    snapshot_root = work / "snapshot"
    (snapshot_root / "src").mkdir(parents=True)
    for path in (SOURCE_PATH, TARGET_PATH):
        (snapshot_root / path).write_text(source)
    files = {p: hashlib.sha256((snapshot_root / p).read_bytes()).hexdigest() for p in (SOURCE_PATH, TARGET_PATH)}
    manifest = SnapshotManifest(source_sha=sha, files=files, editable_paths=[TARGET_PATH])
    task = prepare_bucket_policy_task("catalog", source, BUCKET, source_sha=sha)
    spec = PlanSpec("plan-local-rke2", "tenant-local", "chat", "env-aws", "env-onprem", "a" * 64, "b" * 64)
    store = PlanStore(work / "t10.db")
    store.register_plan(spec, run_id="local", occurred_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    with store._connection() as connection:  # T10's state machine owns this transition in a deployment.
        connection.execute("UPDATE plans SET state='REHEARSE' WHERE plan_id=?", (spec.plan_id,))
    records = AttemptStore(work / "records", os.urandom(32))
    kubectl_, admin = Kubectl(), Kube()
    coordinators = {}

    class Provider:
        def __init__(self, effect):
            self.effect = effect

        async def propose(self, bundle, tools):
            # Scripted stand-in for the agent's cleanup attempt; T11 must refuse it.
            with suppress(PolicyDenied):
                tools.dispatch("rehearsal.exec", {"command": f"aws s3 rb s3://{BUCKET} --force"})
            tools.dispatch("mcp__bravo__repo_read", {"path": SOURCE_PATH})
            target = tools.dispatch("mcp__bravo__repo_read", {"path": TARGET_PATH})
            tools.dispatch("mcp__bravo__plan_propose", {
                "run_id": bundle.run_id, "source_sha": bundle.source_sha, "risk": "low",
                "rationale": "Translate the S3 policy for the SeaweedFS test bucket",
                "edits": [{"path": TARGET_PATH, "before_sha256": target["sha256"],
                           "replacement": policy(self.effect)}]})

    class Planner:
        def binding(self, admission):
            return PlanBinding(spec.plan_digest, spec.target_environment_id)

        def factory(self, admission, bound_task):
            class Factory:
                def prepare(self, attempt, feedback):
                    run_id = bound_task.run_id
                    if attempt == 1:
                        bundle = FailureBundle(run_id=run_id, source_sha=sha, failure_code="POLICY_TRANSLATION",
                                               evidence_digest="0" * 64,
                                               summary=bucket_summary(bound_task, SOURCE_PATH, TARGET_PATH))
                        coordinators[run_id] = Coordinator(bundle, Snapshot(snapshot_root, manifest))
                    coordinator = coordinators[run_id]
                    if attempt > 1:
                        failure = coordinator.bundle.model_copy(update={
                            "evidence_digest": feedback, "failure_code": "TEMPLATE_TEST_FAILED",
                            "summary": "Independent verifier: anonymous read was still allowed."})
                        coordinator.verify(VerificationReceipt(
                            run_id=run_id, source_sha=sha, proposal_digest=coordinator.result()["proposal_digest"],
                            attempt=attempt - 1, verdict="FAIL", effects_known=True, evidence_digest=feedback,
                            failure=failure))
                    effect = first if attempt == 1 else "restricted"
                    proposer = T11BucketProposer(coordinator, Provider(effect), SOURCE_PATH, TARGET_PATH,
                                                 attempt=attempt, feedback_digest=feedback)
                    lifecycle = AccessRule(NamespaceLifecycle(kubectl_, PatientCleaner(admin), profile, attempt))
                    job = SeaweedBucketJob(admin, BoundBucketProposalSource(coordinator, TARGET_PATH),
                                           coordinator.snapshot, fixture, lambda: credential,
                                           source_policy_path=SOURCE_PATH, deadline_seconds=900)
                    verifier = BucketVerifier(bound_task, job, attempt)
                    gate = T3Gate(kubectl_, targets(profile), BUSYBOX, cleanup_timeout=300)
                    return AttemptPorts(proposer, lifecycle, gate, verifier)

            return Factory()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    workload = WorkloadSender("https://issuer.invalid", "t10-local", "t15-runner", key.public_key(), spec.tenant_id)

    def token():
        now = int(time.time())
        return jwt.encode({"iss": "https://issuer.invalid", "aud": "t10-local", "sub": "t15-runner",
                           "iat": now, "exp": now + 300}, key, algorithm="RS256")

    evidence = RecordedEvidence(records)
    intake = ChangeIntake(store, sender=workload, read_original=evidence.read_original,
                          authorize_hold=lambda _c, who, *_: who == "local-security-fixture")
    deliveries = []
    sender = RecordedAttemptSender(records, Planner(), deliver=lambda s: deliveries.append(
        submit_candidate(s, candidate_type=Candidate, intake=intake, token_supplier=token).state))
    service = RehearsalService(store, host=T15RehearsalHost(Catalog(task), sender, evidence),
                               check_start=lambda *_: None)
    operator = Principal("local-operator", spec.tenant_id)
    status = None
    for attempt in (1, 2, 3):
        started = time.monotonic()
        receipt = service.start(operator, spec.plan_id, task_id="bucket", expected_version=1,
                                attempt=attempt, command_key=f"start-{attempt}")
        result = service.reconcile(operator, spec.plan_id, rehearsal_id=receipt["rehearsal_id"], attempt=attempt)
        record = records.read(receipt["rehearsal_id"], attempt) or {}
        report["attempts"].append({
            "attempt": attempt, "seconds": round(time.monotonic() - started), "t10_status": result["status"],
            "record_verdict": record.get("verdict"), "record_reason": record.get("reason"),
            "record_detail": record.get("detail"),
            "verification_id": record.get("verification_id"), "namespace": record.get("namespace"),
            "created_uid": record.get("created_uid"), "removed_uid": record.get("removed_uid"),
            "isolation_probe_run_id": record.get("isolation_probe_run_id"),
            "diff_hash": record.get("diff_hash"), "image_digest": record.get("image_digest"),
            "evidence": record.get("evidence"),
            "tool_events_status": record.get("tool_events_status"),
            "t11_denials": [e for e in record.get("tool_events", []) if e["outcome"] == "DENIED"]})
        status = result["status"]
        if status != "FAIL":
            break
        intake.hold("local-security-fixture", spec.plan_id, result["verification_id"], decision_ref=f"retry-{attempt}")
    report["deliveries"] = deliveries
    report["rehearsal_id"] = receipt["rehearsal_id"]
    report["leftover_namespaces"] = [n["metadata"]["name"] for n in get("namespaces")["items"]
                                     if n["metadata"]["name"].startswith("rehearsal-")]
    if status == "PASS":
        with store._connection() as connection:
            current = intake.current(connection, operator, {"plan_id": spec.plan_id, "version": 1},
                                     rehearsal_id=receipt["rehearsal_id"])
        report["awaiting_approval_binding"] = current.binding
        approval = ChangeApprovalService(store, authority=intake.current).approve(
            operator, spec.plan_id, run_id=receipt["rehearsal_id"], binding=current.binding,
            expected_version=1, command_key="approve-local")
        report["fixture_operator_approval"] = {k: approval[k] for k in ("approval_kind", "run_id", "binding")}
    report["success"] = status == "PASS" and not report["leftover_namespaces"]
    out = state / f"t15-rehearsal-flow-{stamp}.json"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
    print(json.dumps({"success": report["success"], "status": status, "report": str(out),
                      "attempts": [(a["attempt"], a["t10_status"], a["record_reason"], a["record_detail"], a["seconds"])
                                   for a in report["attempts"]]}))
    return 0 if report["success"] else 1


class Catalog:
    def __init__(self, task):
        self.task = task

    def for_plan(self, connection, plan, task_id):
        if task_id != "bucket":
            raise ValueError("unknown task")
        return self.task

    def for_admission(self, admission):
        return self.task


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["setup", "run"])
    parser.add_argument("--state", required=True, type=pathlib.Path, help="private output directory")
    parser.add_argument("--first-proposal", choices=["restricted", "overbroad"], default="overbroad")
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "setup":
        setup(args.state)
        sys.exit(0)
    sys.exit(run(args.state, args.first_proposal))
