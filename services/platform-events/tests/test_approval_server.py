"""Server refuses missing startup authority without printing private diagnostics."""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_intake_deployment import binding as binding
from test_intake_deployment import key as key
from test_intake_deployment import token as workload_token
from test_operator_access import Identity
from test_t10_recorded_flow import flow as flow

from bravo_platform_events import approval_server
from bravo_platform_events.trusted_rehearsal import TrustedHostPorts


def test_load_host_uses_separate_mounted_keys_and_no_startup_execution(flow, binding, key, tmp_path):
    workload_binding, jwks = binding
    human = Identity(key)
    public_file, record_key, config_file = [tmp_path / name for name in ("public.json", "hmac", "host.json")]
    public_file.write_text(json.dumps(jwks))
    record_key.write_bytes(b"r" * 32)
    records = flow["path"]
    config = dict(version=1, workload_binding=workload_binding,
        public_jwks_file=str(public_file), records_root=str(records), record_key_file=str(record_key),
        operator_verifier=dict(issuer=human.verifier.issuer, audience="bravo-platform",
            client_id="console", assurance="interactive-mfa", subjects={"human-1": "tenant-1"},
            public_jwks={"keys": [human.public]}))
    config_file.write_text(json.dumps(config))
    ports = TrustedHostPorts(SimpleNamespace(submit=lambda *_: None, read=lambda *_: None),
        {"env-aws": "source", "env-onprem": "target"}, flow["service"].host.catalog,
        flow["planner"], lambda *_a, **_k: None)
    host = approval_server.load_host(flow["store"], config_file, lambda store: ports)
    host.intake.sender.authenticate(workload_token(key))
    now = int(time.time())
    assert host.intake.authorize_hold.verifier.verify(
        human.token(iat=now, nbf=now, exp=now+300, auth_time=now)) is not None
    assert host.evidence.store is host.rehearsals.evidence.store
    with TestClient(host.app, base_url="https://host.invalid") as client:
        assert client.post("/v1/plans/plan-fixture/rehearsals",
            headers={"Idempotency-Key": "unauthenticated"},
            json={"task_id": "bucket", "attempt": 1, "expected_version": 1}).status_code == 403
    assert flow["planner"].events == []
    config["records_root"] = str(tmp_path / "missing-volume")
    config_file.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="approval_host_mounts_required"):
        approval_server.load_host(flow["store"], config_file, lambda store: pytest.fail("missing volume must fail first"))


def test_missing_tls_and_bad_factory_fail_before_db_or_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(approval_server, "runtime_connection", lambda: pytest.fail("unexpected DB access"))
    for factory in ("test_approval_server:create_ports", "x:PRIVATE_SENTINEL/../bad"):
        with pytest.raises(SystemExit, match="^approval_startup_unavailable$"):
            approval_server.main(["--config", str(tmp_path/"config"), "--ports", factory,
                                  "--tls-cert", str(tmp_path/"missing"), "--tls-key", str(tmp_path/"missing")])
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().out


def test_serve_uses_single_process_tls_and_suppresses_proxy_and_access_logs(tmp_path, monkeypatch):
    certificate = tmp_path / "certificate"
    certificate.write_text("fixture: uvicorn is replaced in this startup test")
    class Transaction:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def execute(self, *_):
            return self
        def fetchall(self):
            return [{"version": 3}]
    store = SimpleNamespace(_transaction=Transaction)
    def factory(_store):
        return None
    monkeypatch.setattr(approval_server.importlib, "import_module", lambda _: SimpleNamespace(create=factory))
    monkeypatch.setattr(approval_server, "runtime_connection", lambda: "fixture-connection")
    monkeypatch.setattr(approval_server, "connection_checks", lambda _: None)
    monkeypatch.setattr(approval_server, "PostgresPlanStore", lambda _: store)
    app = object()
    monkeypatch.setattr(approval_server, "load_host", lambda *_: SimpleNamespace(app=app))
    calls = []
    monkeypatch.setattr(approval_server.uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    approval_server.main(["--config", "fixture", "--ports", "reviewed_module:create",
                          "--tls-cert", str(certificate), "--tls-key", str(certificate)])
    assert len(calls) == 1 and calls[0][0] == (app,)
    assert calls[0][1]["workers"] == 1
    assert calls[0][1]["ssl_certfile"] == str(certificate)
    assert calls[0][1]["access_log"] is False and calls[0][1]["proxy_headers"] is False
