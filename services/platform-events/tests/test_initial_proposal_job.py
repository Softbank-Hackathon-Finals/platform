"""Platform Job carrier: one deterministic Job, pinned Job identity, sealed receipt only."""

import json
import time
from types import SimpleNamespace

import jwt
import pytest
import test_initial_proposal as fixture_module
from bravo_agent.contracts import Proposal, digest
from bravo_agent.product_execution_contracts import ExecutionPending, ExecutionUnknown
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from fastapi.testclient import TestClient
from test_cutover import SPEC
from test_initial_proposal import ACTOR, RUNTIME, SOURCE, Fixture

from bravo_platform_events import initial_proposal_job as job
from bravo_platform_events.initial_proposal import InitialIntent
from bravo_platform_events.initial_proposal_api import (
    PATH,
    AuthenticatedInitialReader,
    InitialRead,
    create_initial_read_app,
)
from bravo_platform_events.initial_proposal_transport import (
    initial_receipt_binding,
    open_initial_receipt,
)
from bravo_platform_events.product_api import KubernetesJobCaller
from bravo_platform_events.workload_identity import WorkloadSender

ISSUER = "https://platform-rke2.invalid"
SUBJECT = f"system:serviceaccount:{job.NAMESPACE}:{job.SERVICE_ACCOUNT}"
IMAGE = "ghcr.io/softbank-hackathon-finals/platform-product-reader@sha256:" + "a" * 64


@pytest.fixture
def api(backend, tmp_path, monkeypatch):
    key = generate_private_key(public_exponent=65537, key_size=2048)
    caller = KubernetesJobCaller(WorkloadSender(ISSUER, job.AUDIENCE, SUBJECT, key.public_key(), SPEC.tenant_id))
    monkeypatch.setattr(fixture_module, "ACTOR", caller.principal)
    f = fixture_module.Fixture(backend, tmp_path)
    intents = []

    def submit(intent):
        intents.append(intent)
        raise ExecutionPending()

    f.client = SimpleNamespace(recipient=lambda _: "d" * 64, submit=submit,
                              read=lambda _: pytest.fail("unexpected read"), validate=lambda *_: None)
    f.port = f.execution()
    with pytest.raises(ExecutionPending):
        f.execute()
    intent = intents[0]
    run_id = job.execution_id(intent.request_id)
    request = InitialRead(principal=caller.principal, intent=intent, github_run_id=run_id)
    now = int(time.time())
    claims = dict(iss=ISSUER, aud=job.AUDIENCE, sub=SUBJECT, iat=now, exp=now + 600,
                  **{"kubernetes.io": {"namespace": job.NAMESPACE,
                                       "pod": {"name": f"{job.job_name(intent.request_id)}-ab12c", "uid": "u"},
                                       "serviceaccount": {"name": job.SERVICE_ACCOUNT, "uid": "s"}}})
    return AuthenticatedInitialReader(f.records, caller), request, key, claims


def post(api, claims=None):
    reader, request, key, base = api
    token = jwt.encode(claims or base, key, algorithm="RS256")
    with TestClient(create_initial_read_app(reader), base_url="https://reader.invalid") as client:
        return client.post(PATH, json=request.model_dump(), headers={"Authorization": "Bearer " + token})


def test_bound_job_pod_reads_its_current_initial_input(api):
    response = post(api)
    assert response.status_code == 200 and response.json()["binding"]["stage"] == "PROPOSE"


@pytest.mark.parametrize("change", [
    lambda c: c["kubernetes.io"]["pod"].update(name="bravo-initial-1-ab12c"),
    lambda c: c["kubernetes.io"]["pod"].update(name="other-pod-ab12c"),
    lambda c: c["kubernetes.io"].update(namespace="default"),
    lambda c: c.pop("kubernetes.io"),
    lambda c: c.update(sub="system:serviceaccount:bravo-initial:default"),
    lambda c: c.update(aud="sts.amazonaws.com"),
    lambda c: c.update(exp=c["iat"] + 7200),
])
def test_other_pod_namespace_account_or_audience_is_forbidden(api, change, monkeypatch):
    monkeypatch.setattr(api[0].records, "_read", lambda *a, **k: pytest.fail("unauthenticated read"))
    claims = json.loads(json.dumps(api[3]))
    change(claims)
    assert post(api, claims).status_code == 403


def test_job_token_cannot_read_under_another_principal(api, monkeypatch):
    reader, request, key, claims = api
    monkeypatch.setattr(reader.records, "_read", lambda *a, **k: pytest.fail("read under another principal"))
    # Another principal (e.g. the GitHub caller's) with an intent consistently re-derived for it,
    # so only the token-to-principal binding can refuse it.
    principal = "e" * 64
    fields = request.intent.model_dump(exclude={"request_id", "recipient_public_key"})
    intent = request.intent.model_copy(update={"request_id": "t10p-" + digest(fields | {"principal": principal})})
    other = request.model_copy(update={"principal": principal, "intent": intent})
    token = jwt.encode(claims, key, algorithm="RS256")
    with TestClient(create_initial_read_app(reader), base_url="https://reader.invalid") as client:
        response = client.post(PATH, json=other.model_dump(), headers={"Authorization": "Bearer " + token})
    assert response.status_code == 403


class Kubectl:
    """Synthetic API server; never runs kubectl."""

    def __init__(self):
        self.creates, self.jobs, self.logs, self.lose_create = [], {}, b"", False

    def __call__(self, args, payload=None):
        if args[0] == "create":
            manifest = json.loads(payload)
            self.creates.append(manifest)
            name = manifest["metadata"]["name"]
            if name in self.jobs:
                return 1, b"", b'Error from server (AlreadyExists): jobs.batch "x" already exists'
            created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            stored = json.loads(payload)
            self.jobs[name] = stored | {"status": {}, "metadata": stored["metadata"] | {
                "creationTimestamp": created, "uid": "job-uid"}}
            # What the API server adds on its own; the operator check must tolerate it.
            pod = self.jobs[name]["spec"]["template"]["spec"]
            pod.update(schedulerName="default-scheduler", terminationGracePeriodSeconds=30,
                       serviceAccount=pod["serviceAccountName"])
            pod["containers"][0].update(imagePullPolicy="IfNotPresent", terminationMessagePolicy="File",
                                        terminationMessagePath="/dev/termination-log")
            pod["volumes"][0]["projected"]["defaultMode"] = 420
            if self.lose_create:
                return 1, b"", b"Unable to connect to the server"
            return 0, b"job.batch/" + name.encode(), b""
        if args[:2] == ["get", "jobs"]:  # the one-execution-at-a-time slot check
            return 0, json.dumps({"items": list(self.jobs.values())}).encode(), b""
        if args[:2] == ["get", "job"]:
            return (0, json.dumps(self.jobs[args[2]]).encode(), b"") if args[2] in self.jobs else (1, b"", b"NotFound")
        if args[0] == "logs":
            return 0, self.logs, b""
        raise AssertionError(args)


@pytest.fixture
def operator(backend, tmp_path):
    f = Fixture(backend, tmp_path)
    fields = f.port.preparation_fields(SPEC.plan_id, 3, **f.ref)
    kube = Kubectl()
    client = job.JobInitialProposal(tmp_path / "operator", image=IMAGE, source_revision=RUNTIME, kubectl=kube)
    intent = InitialIntent(**fields, recipient_public_key=client.recipient(fields["request_id"]))
    value = f.records.read_input(ACTOR, intent.plan_id, intent.plan_version,
                                input_ref=intent.input_ref, input_digest=intent.input_digest)
    run_id = job.execution_id(intent.request_id)
    proposal = Proposal(run_id=value["run_id"], source_sha=SOURCE, risk="low",
        rationale="Synthetic initial proposal", edits=[dict(path="app/config.py",
            before_sha256=value["manifest"]["files"]["app/config.py"], replacement="VALUE = 2\n")])
    result = dict(request_id=intent.request_id, input_digest=intent.input_digest,
        runtime_source_sha=RUNTIME, execution_receipt_id=f"github-{run_id}-1", proposal=proposal.model_dump(),
        proposal_digest=digest(proposal), schema_valid=True, lint_passed=True)
    return client, kube, intent, result, tmp_path


def complete(kube, intent, result, tmp_path, status=200):
    token = tmp_path / "token"
    token.write_text("header.payload.signature\n")
    env = {"PRODUCT_INTENT": json.dumps(intent.model_dump(mode="json")),
           "PRODUCT_EXECUTION_ID": job.execution_id(intent.request_id), "PRODUCT_BROKER_CA_PEM": "unused"}
    calls = []

    def broker(body):
        calls.append(body)
        return status, result
    receipt, sealed = job.run(env, call=broker, token_file=token)
    assert len(calls) == 1 and calls[0]["web_identity_token"] == "header.payload.signature"
    kube.logs = json.dumps(sealed).encode()
    kube.jobs[job.job_name(intent.request_id)]["status"] = {"succeeded": 1}
    return receipt


def test_one_job_then_sealed_receipt_from_its_log(operator):
    client, kube, intent, result, tmp_path = operator
    with pytest.raises(ExecutionPending):
        client.submit(intent)
    manifest = kube.creates[0]
    spec = manifest["spec"]["template"]["spec"]
    assert manifest["spec"]["backoffLimit"] == 0 and spec["automountServiceAccountToken"] is False
    assert spec["containers"][0]["image"] == IMAGE and spec["hostNetwork"] is True
    assert "AWS" not in json.dumps(manifest) and "secretKeyRef" not in json.dumps(manifest)
    receipt = complete(kube, intent, result, tmp_path)
    assert client.read(intent) == receipt and client.submit(intent) == receipt
    assert len(kube.creates) == 1


def test_lost_create_response_reads_same_job_and_never_creates_twice(operator):
    client, kube, intent, result, tmp_path = operator
    kube.lose_create = True
    with pytest.raises(ExecutionUnknown):
        client.submit(intent)
    with pytest.raises(ExecutionPending):
        client.submit(intent)
    complete(kube, intent, result, tmp_path)
    assert client.read(intent)["status"] == 200 and len(kube.creates) == 1


@pytest.mark.parametrize("change", [
    lambda j: j["spec"].update(backoffLimit=1),
    lambda j: j["spec"]["template"]["spec"]["containers"][0].update(image=IMAGE[:-1] + "b"),
    lambda j: j["metadata"]["annotations"].update({"bravo.dev/request-digest": "0" * 64}),
    lambda j: j.update(status={"failed": 1}),
])
def test_replaced_or_failed_job_never_delivers(operator, change):
    client, kube, intent, result, tmp_path = operator
    with pytest.raises(ExecutionPending):
        client.submit(intent)
    complete(kube, intent, result, tmp_path)
    change(kube.jobs[job.job_name(intent.request_id)])
    with pytest.raises(ExecutionUnknown):
        client.read(intent)


@pytest.mark.parametrize("change", [
    lambda c: c.update(command=["python", "-c", "print('forged')"]),
    lambda c: c.update(env=[{**e, "value": "{}"} if e["name"] == "PRODUCT_INTENT" else e for e in c["env"]]),
    lambda c: c.update(securityContext={**c["securityContext"], "privileged": True}),
    lambda c: c["volumeMounts"].append({"name": "host", "mountPath": "/host"}),
])
def test_same_name_job_with_other_container_spec_never_delivers(operator, change):
    client, kube, intent, result, tmp_path = operator
    with pytest.raises(ExecutionPending):
        client.submit(intent)
    complete(kube, intent, result, tmp_path)
    change(kube.jobs[job.job_name(intent.request_id)]["spec"]["template"]["spec"]["containers"][0])
    with pytest.raises(ExecutionUnknown):
        client.read(intent)


@pytest.mark.parametrize("change", [
    lambda p: p.update(serviceAccountName="default", serviceAccount="default"),
    lambda p: p.update(serviceAccount="default"),  # the legacy alias must not name another account
    lambda p: p.update(automountServiceAccountToken=True),
    lambda p: p.update(hostPID=True),
    lambda p: p.update(initContainers=[{"name": "x", "image": IMAGE}]),
    lambda p: p["volumes"].append({"name": "host", "hostPath": {"path": "/"}}),
    lambda p: p["volumes"][0]["projected"]["sources"][0]["serviceAccountToken"].update(audience="sts.amazonaws.com"),
    lambda p: p["volumes"][0]["projected"]["sources"][0]["serviceAccountToken"].update(expirationSeconds=86400),
])
def test_same_name_job_with_other_pod_spec_never_delivers(operator, change):
    client, kube, intent, result, tmp_path = operator
    with pytest.raises(ExecutionPending):
        client.submit(intent)
    complete(kube, intent, result, tmp_path)
    change(kube.jobs[job.job_name(intent.request_id)]["spec"]["template"]["spec"])
    with pytest.raises(ExecutionUnknown):
        client.read(intent)


@pytest.mark.parametrize("change", [
    lambda c: c.update(command=["python", "-c", "print('forged')"]),
    lambda c: c.update(env=[{**e, "value": "{}"} if e["name"] == "PRODUCT_INTENT" else e for e in c["env"]]),
    None,  # Identical spec, but created before the operator's ledger marker.
])
def test_preempted_job_name_is_rejected_without_model_call(operator, change):
    client, kube, intent, result, tmp_path = operator
    forged = job.job_manifest(intent, IMAGE)
    if change:
        change(forged["spec"]["template"]["spec"]["containers"][0])
    name = forged["metadata"]["name"]
    forged["metadata"]["creationTimestamp"] = "2026-01-01T00:00:00Z"  # well before the marker
    kube.jobs[name] = forged | {"status": {"succeeded": 1}}
    receipt = job.run({"PRODUCT_INTENT": json.dumps(intent.model_dump(mode="json")),
                       "PRODUCT_EXECUTION_ID": job.execution_id(intent.request_id),
                       "PRODUCT_BROKER_CA_PEM": "unused"},
                      call=lambda _: (200, result), token_file=_token(tmp_path))[1]
    kube.logs = json.dumps(receipt).encode()  # A hand-sealed receipt; the seal needs only the public key.
    with pytest.raises(ExecutionUnknown, match="JOB_NAME_PREEMPTED"):
        client.submit(intent)
    with pytest.raises(ExecutionUnknown, match="JOB_BINDING_CONFLICT"):
        client.read(intent)
    assert len(kube.creates) == 1


def _token(tmp_path):
    token = tmp_path / "forged-token"
    token.write_text("header.payload.signature\n")
    return token


def test_broker_rejection_is_sealed_evidence_not_a_result(operator):
    client, kube, intent, result, tmp_path = operator
    with pytest.raises(ExecutionPending):
        client.submit(intent)
    receipt = complete(kube, intent, {"state": "NEEDS_HUMAN", "reason": "PRODUCT_ACCESS_DENIED"}, tmp_path, 403)
    assert receipt["status"] == 403
    with pytest.raises(ExecutionUnknown):
        client.read(intent)
    run_id = job.execution_id(intent.request_id)
    opened = open_initial_receipt(intent, run_id, json.loads(kube.logs), client._private_key(intent.request_id))
    assert opened["status"] == 403 and opened.keys() >= initial_receipt_binding(intent, run_id).keys()


def test_execution_id_is_stable_and_bound(operator):
    _, _, intent, _, tmp_path = operator
    assert job.execution_id(intent.request_id) == job.execution_id(intent.request_id)
    env = {"PRODUCT_INTENT": json.dumps(intent.model_dump(mode="json")), "PRODUCT_EXECUTION_ID": "1234",
           "PRODUCT_BROKER_CA_PEM": "unused"}
    with pytest.raises(ValueError):
        job.run(env, call=lambda _: pytest.fail("unbound call"), token_file=tmp_path / "missing")
    with pytest.raises(ValueError):
        job.job_manifest(intent, "ghcr.io/example/reader:latest")


def test_default_job_image_is_the_gitops_reader_pin():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    image, revision = job.gitops_image(root / "infra/gitops/environments/development/product-reader-image.json")
    assert image.startswith("ghcr.io/softbank-hackathon-finals/platform-product-reader@sha256:")
    assert len(revision) == 40


def test_image_from_another_source_revision_is_never_run(operator):
    client, kube, intent, _, _ = operator
    other = job.JobInitialProposal(client.root, image=IMAGE, source_revision="d" * 40, kubectl=kube)
    with pytest.raises(ExecutionUnknown, match="JOB_IMAGE_SOURCE_MISMATCH"):
        other.submit(intent)
    assert kube.creates == []


def test_one_execution_at_a_time_records_nothing_while_busy(operator):
    client, kube, intent, _, _ = operator
    kube.jobs["bravo-initial-9"] = {"metadata": {"name": "bravo-initial-9"}, "status": {"active": 1}}
    with pytest.raises(ExecutionPending, match="JOB_SLOT_BUSY"):
        client.submit(intent)
    assert kube.creates == []
    with pytest.raises(ExecutionUnknown, match="EXECUTION_NOT_SUBMITTED"):
        client.read(intent)  # no ledger row: a later submit is a first send
    kube.jobs["bravo-initial-9"]["status"] = {"succeeded": 1}
    with pytest.raises(ExecutionPending):
        client.submit(intent)
    assert len(kube.creates) == 1


def test_operator_clock_ahead_of_the_api_server_within_skew_still_reads(operator, monkeypatch):
    from datetime import UTC, datetime, timedelta

    client, kube, intent, result, tmp_path = operator
    with pytest.raises(ExecutionPending):
        client.submit(intent)
    complete(kube, intent, result, tmp_path)
    name = job.job_name(intent.request_id)
    ahead = datetime.now(UTC) - timedelta(seconds=45)  # API server 45 s behind the operator
    kube.jobs[name]["metadata"]["creationTimestamp"] = ahead.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert client.read(intent)["status"] == 200
    kube.jobs[name]["metadata"]["creationTimestamp"] = (ahead - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with pytest.raises(ExecutionUnknown):
        client.read(intent)


def test_job_reader_refuses_plain_http_duplicate_fields_and_oversized_body(api, monkeypatch):
    reader, request, key, claims = api
    monkeypatch.setattr(reader.records, "_read", lambda *a, **k: pytest.fail("invalid read"))
    headers = {"Authorization": "Bearer " + jwt.encode(claims, key, algorithm="RS256"),
               "Content-Type": "application/json"}
    with TestClient(create_initial_read_app(reader), base_url="http://reader.invalid") as client:
        assert client.post(PATH, json=request.model_dump(), headers=headers).status_code == 403
    with TestClient(create_initial_read_app(reader), base_url="https://reader.invalid") as client:
        assert client.post(PATH, content='{"principal":"a","principal":"b"}', headers=headers).status_code == 422
        assert client.post(PATH, content="x" * 16385, headers=headers).status_code == 413
