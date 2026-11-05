"""Mounted host authority is checked before creating any remote-effect ports."""

import json

import pytest
import test_registration_sso as sso
from fastapi.testclient import TestClient
from test_intake_deployment import binding as binding
from test_intake_deployment import key as key
from test_intake_deployment import token as workload_token
from test_t10_recorded_flow import flow as flow

from bravo_platform_events import rehearsal_server
from bravo_platform_events.rehearsal_server import RehearsalPorts
from bravo_platform_events.rehearsal_sso import AUDIENCE


@pytest.fixture
def config(flow, binding, tmp_path):
    workload_binding, jwks = binding
    public, signing, grants, path = [tmp_path / n for n in ("public.json", "key", "grants.json", "host.json")]
    public.write_text(json.dumps(jwks))
    signing.write_bytes(b"r" * 32)
    grants.write_text('{"version":1,"grants":[]}')
    value = dict(version=1, workload_binding=workload_binding, public_jwks_file=str(public),
        records_root=str(flow["path"]), record_key_file=str(signing), grants_file=str(grants),
        sso_verifier=sso.CONFIG | {"audience": AUDIENCE, "tenant_id": workload_binding["tenant_id"]})
    path.write_text(json.dumps(value))
    return path, value


def test_load_binds_mounted_workload_keys_and_denies_missing_human_proof(flow, config, key):
    path, _ = config
    calls = []

    def ports(store):
        calls.append(store)
        return RehearsalPorts(flow["service"].host.catalog, flow["planner"])

    host = rehearsal_server.load_host(flow["store"], path, ports)
    assert calls == [flow["store"]]
    host.intake.sender.authenticate(workload_token(key))
    assert host.evidence.store.root == flow["records"].root
    with TestClient(host.app, base_url="https://host.invalid") as client:
        result = client.post("/v1/plans/plan-fixture/rehearsals", headers={"Idempotency-Key": "start"},
            json={"task_id": "bucket", "expected_version": 1, "attempt": 1})
        assert result.status_code == 403
    assert flow["planner"].events == []


@pytest.mark.parametrize("fault", ["tenant", "audience", "grant-mount", "record-volume", "key", "relative-path"])
def test_bad_mounts_and_identity_stop_before_port_factory(flow, config, fault, tmp_path):
    path, value = config
    if fault == "tenant":
        value["sso_verifier"]["tenant_id"] = "other-tenant"
    elif fault == "audience":
        value["sso_verifier"]["audience"] = "bravo-platform-registration"
    elif fault == "grant-mount":
        value["grants_file"] = str(tmp_path / "missing-grants")
    elif fault == "record-volume":
        value["records_root"] = str(tmp_path / "missing-volume")
    elif fault == "key":
        from pathlib import Path
        Path(value["record_key_file"]).write_bytes(b"short")
    else:
        value["public_jwks_file"] = "relative.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        rehearsal_server.load_host(flow["store"], path,
            lambda _: pytest.fail("invalid authority must fail before port construction"))
    assert flow["planner"].events == []


def test_bad_startup_is_private_and_does_not_access_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rehearsal_server, "runtime_connection", lambda: pytest.fail("unexpected DB access"))
    for factory in ("reviewed_module:create", "x:PRIVATE_SENTINEL/../bad"):
        with pytest.raises(SystemExit, match="^rehearsal_startup_unavailable$"):
            rehearsal_server.main(["--config", str(tmp_path / "missing"), "--ports", factory,
                "--tls-cert", str(tmp_path / "missing"), "--tls-key", str(tmp_path / "missing")])
    output = capsys.readouterr()
    assert "PRIVATE_SENTINEL" not in output.out + output.err
