"""IAM-01.1 on a registered S3-compatible endpoint (the target's SeaweedFS)."""

import importlib.util
import io
import json
import os
import secrets
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from bravo_controls import Catalog, Environment, Runner
from bravo_controls.object_endpoint import (
    EndpointAnonymousReadProbe,
    EndpointObjectTarget,
    endpoint_clients,
)
from bravo_controls.runner import RunContext, TestSpec

ROOT = Path(__file__).resolve().parents[3]
TARGET_ENV = Environment("onprem-target", "target", "onprem:k8s/target-dev")
NOW = datetime(2026, 11, 7, 8, tzinfo=UTC)
SPEC = TestSpec("IAM-01.1", "IAM-01", "1", "deny", "required")
SEAWEEDFS = "docker.io/chrislusf/seaweedfs:4.52@sha256:57877e32bb90073b37dc109b438ba66e84fd5ed9ced5f5c04f56444ef243ce59"


def target(endpoint="https://objects.target.example:8333"):
    return EndpointObjectTarget("demo-chat", TARGET_ENV, endpoint, "demo-chat-attachments", "attachments/canary.txt")


def probe_for(registered, *, sink, app_key="client-access", secret="client-secret", clock=lambda: NOW):
    app, anonymous = endpoint_clients(registered, access_key_id=app_key, secret_access_key=secret)
    return EndpointAnonymousReadProbe(registered, app_client=app, anonymous_client=anonymous, app_identity="client",
                                      evidence_sink=sink, clock=clock), app, anonymous


def body():
    return {"Body": StreamingBody(io.BytesIO(b""), 0), "ResponseMetadata": {"HTTPStatusCode": 200}}


def stub(client, outcomes):
    stubber = Stubber(client)
    for outcome in outcomes:
        if outcome == "allow":
            stubber.add_response("get_object", body(), {"Bucket": "demo-chat-attachments", "Key": "attachments/canary.txt"})
        else:
            stubber.add_client_error("get_object", service_error_code=outcome, http_status_code=403)
    stubber.activate()
    return stubber


@pytest.mark.parametrize(("anonymous", "observed"), [("AccessDenied", "deny"), ("allow", "allow")])
def test_the_anonymous_get_decides_and_the_app_get_brackets_it(anonymous, observed):
    kept = []
    probe, app, anon = probe_for(target(), sink=kept.append)
    stub(app, ["allow", "allow"] if observed == "deny" else ["allow"])
    stub(anon, [anonymous])
    result = probe(RunContext("run-1", "demo-chat", TARGET_ENV), SPEC)
    assert (result.observed, result.positive_control, result.tool) == (observed, "pass", "bravo-controls-s3-endpoint")
    evidence = json.loads(kept[0])
    assert evidence["environment"]["role"] == "target" and evidence["action"] == "s3:GetObject"
    assert evidence["postpositive"]["outcome"] == ("allow" if observed == "deny" else None)
    assert "client-access" not in kept[0].decode() and "client-secret" not in kept[0].decode()


def test_an_app_answer_without_a_closable_body_is_not_an_allow():
    kept = []
    probe, app, anon = probe_for(target(), sink=kept.append)
    stubber = Stubber(app)
    stubber.add_response("get_object", {"ResponseMetadata": {"HTTPStatusCode": 200}},
                         {"Bucket": "demo-chat-attachments", "Key": "attachments/canary.txt"})
    stubber.activate()
    result = probe(RunContext("run-1", "demo-chat", TARGET_ENV), SPEC)
    assert (result.observed, result.positive_control) == (None, "fail")


def test_a_failed_app_get_is_never_a_pass():
    kept = []
    probe, app, anon = probe_for(target(), sink=kept.append)
    stub(app, ["NoSuchKey"])
    result = probe(RunContext("run-1", "demo-chat", TARGET_ENV), SPEC)
    assert (result.observed, result.positive_control, result.unknown_reason) == (None, "fail", "POSITIVE_CONTROL_FAILED")


def test_the_demo_gate_run_reports_the_other_tests_as_not_run():
    documents = (json.loads((ROOT / "docs/controls/profiles.json").read_text()),
                 json.loads((ROOT / "docs/controls/control-result.schema.json").read_text()))
    kept = []
    probe, app, anon = probe_for(target(), sink=kept.append)
    stub(app, ["allow", "allow"])
    stub(anon, ["AccessDenied"])
    run = Runner(Catalog(*documents), {"IAM-01.1": probe}, clock=lambda: NOW).run(
        profile_id="demo-gate", profile_version="1", app="demo-chat", environment=TARGET_ENV)
    rows = {row["test_id"]: row for row in run["results"]}
    assert rows["IAM-01.1"]["verdict"] == "PASS"
    assert {rows[t]["verdict"] for t in ("SEC-01.1", "AUD-01.1")} == {"UNKNOWN"}


@pytest.mark.parametrize("endpoint", [
    "http://objects.target.example:8333",   # plain HTTP off loopback and outside the cluster
    "http://seaweedfs.target-dev.svc.attacker.example:8333",
    "http://seaweedfs.svc.cluster.local:8333",   # no namespace
    "http://evil.seaweedfs.target-dev.svc:8333",   # not <service>.<namespace>.svc
    "https://objects.target.example:8333/bucket",
    "https://user:pass@objects.target.example:8333",
    "https://objects.target.example:8333?x=1",
    "ftp://objects.target.example",
])
def test_only_a_registered_https_endpoint_is_accepted(endpoint):
    with pytest.raises(ValueError):
        target(endpoint)
    assert target("http://127.0.0.1:8333").endpoint == "http://127.0.0.1:8333"
    # The target app's own endpoint: the Job runs in the cluster, beside the store.
    for inside in ("http://seaweedfs.target-dev.svc.cluster.local:8333", "http://seaweedfs.target-dev.svc:8333"):
        assert target(inside).endpoint == inside


def test_the_operators_aws_profile_and_ca_bundle_do_not_reach_the_endpoint_clients(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_PROFILE", "operator-sso-profile")  # exported for the source run; absent here
    monkeypatch.setenv("AWS_CA_BUNDLE", str(tmp_path / "missing.pem"))
    app, anonymous = endpoint_clients(target(), access_key_id="a", secret_access_key="b")
    assert app.meta.endpoint_url == anonymous.meta.endpoint_url == target().endpoint


def test_clients_must_be_bound_to_the_registered_endpoint_with_path_addressing():
    from botocore.config import Config

    registered = target()
    app, anonymous = endpoint_clients(registered, access_key_id="a", secret_access_key="b")
    other, _ = endpoint_clients(target("https://other.target.example:8333"), access_key_id="a", secret_access_key="b")
    with pytest.raises(ValueError, match="registered endpoint"):
        EndpointAnonymousReadProbe(registered, app_client=other, anonymous_client=anonymous, app_identity="client",
                                   evidence_sink=lambda _: None)
    import botocore.session
    virtual = botocore.session.Session().create_client(
        "s3", region_name="us-east-1", endpoint_url=registered.endpoint, aws_access_key_id="a", aws_secret_access_key="b",
        config=Config(connect_timeout=3, read_timeout=5, retries={"mode": "standard", "total_max_attempts": 2},
                      signature_version="s3v4", s3={"addressing_style": "virtual"}))
    with pytest.raises(ValueError, match="registered endpoint"):
        EndpointAnonymousReadProbe(registered, app_client=virtual, anonymous_client=anonymous, app_identity="client",
                                   evidence_sink=lambda _: None)
    with pytest.raises(ValueError):  # the anonymous client must be unsigned
        EndpointAnonymousReadProbe(registered, app_client=app, anonymous_client=app, app_identity="client",
                                   evidence_sink=lambda _: None)


# --- the target's real SeaweedFS (same image and arguments as target-workloads/objects.py) -------------

def _t9():
    spec = importlib.util.spec_from_file_location("t9_object_exposure", ROOT / "infra/envs/target-workloads/object_exposure.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seaweedfs(tmp_path, s3_json):
    (tmp_path / "s3.json").write_text(json.dumps(s3_json))
    (tmp_path / "s3.json").chmod(0o644)
    port = _free_port()
    name = "t14-seaweedfs-" + secrets.token_hex(4)
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:8333",
                    "-v", f"{tmp_path / 's3.json'}:/etc/seaweedfs-auth/s3.json:ro", "--entrypoint", "/usr/bin/weed",
                    SEAWEEDFS, "-logtostderr=true", "server", "-dir=/data", "-master.dir=/data",
                    "-master.peers=none", "-master.volumeSizeLimitMB=128", "-volume.max=8",
                    "-ip=127.0.0.1", "-ip.bind=127.0.0.1", "-filer", "-s3", "-s3.port=8333", "-s3.ip.bind=0.0.0.0",
                    "-s3.config=/etc/seaweedfs-auth/s3.json", "-s3.iam=false",
                    "-s3.port.iceberg=0", "-s3.port.lance=0"],
                   check=True, capture_output=True, timeout=120)
    return name, f"http://127.0.0.1:{port}"


def _ready(client, bucket):
    for _ in range(60):
        try:
            client.create_bucket(Bucket=bucket)
            return
        except Exception as error:  # the filer and S3 gateway start after the container
            code = (getattr(error, "response", None) or {}).get("Error", {}).get("Code")
            if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                return
            time.sleep(1)
    raise RuntimeError("seaweedfs did not start")


@pytest.mark.skipif(os.environ.get("BRAVO_SEAWEEDFS_DOCKER") != "1", reason="opt-in Docker SeaweedFS qualification")
@pytest.mark.parametrize(("state", "verdict"), [("closed", "PASS"), ("open-demo", "FAIL")])
def test_the_target_seaweedfs_with_the_t9_identity_file(tmp_path, state, verdict):
    t9 = _t9()
    keys = {f"{name}_{part}": secrets.token_hex(12 if part == "ACCESS" else 24)
            for name in ("BOOTSTRAP", "CLIENT") for part in ("ACCESS", "SECRET")}
    environ = {f"BRAVO_OBJECT_{name}_KEY": value for name, value in keys.items()}
    extra = {"acknowledge_open": True, "demo_run_id": "demo-2026-11-08"} if state == "open-demo" else {}
    s3_json = t9.render(state, "demo-chat-attachments", environ, **extra)
    name, endpoint = _seaweedfs(tmp_path, s3_json)
    try:
        registered = target(endpoint)
        admin, _ = endpoint_clients(registered, access_key_id=keys["BOOTSTRAP_ACCESS"],
                                    secret_access_key=keys["BOOTSTRAP_SECRET"])
        _ready(admin, "demo-chat-attachments")
        admin.put_object(Bucket="demo-chat-attachments", Key="attachments/canary.txt", Body=b"canary")
        kept = []
        # the real clock: the Runner accepts only observations collected inside its own call
        probe, _, _ = probe_for(registered, sink=kept.append, clock=lambda: datetime.now(UTC), app_key=keys["CLIENT_ACCESS"],
                                secret=keys["CLIENT_SECRET"])
        documents = (json.loads((ROOT / "docs/controls/profiles.json").read_text()),
                     json.loads((ROOT / "docs/controls/control-result.schema.json").read_text()))
        run = Runner(Catalog(*documents), {"IAM-01.1": probe}).run(
            profile_id="demo-gate", profile_version="1", app="demo-chat", environment=TARGET_ENV)
        [row] = [r for r in run["results"] if r["test_id"] == "IAM-01.1"]
        assert row["verdict"] == verdict, (row, kept)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
