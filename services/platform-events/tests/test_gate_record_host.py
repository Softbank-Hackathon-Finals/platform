"""Current signed identity and protected grants against SQLite/PostgreSQL."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
import test_registration_sso as sso
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_gate_records import bundle, contract, gate_pg, ready, service  # noqa: F401
from test_gate_records import records as records
from test_registration_sso import proof as proof

from bravo_platform_events.gate_record_host import (
    AUDIENCE,
    GateRecordGrants,
    GateRecordHost,
    GateRecordVerifier,
    create_gate_record_app,
    make_gate_record_token,
)
from bravo_platform_events.operator_identity import OperatorPrincipal


@pytest.fixture
def api(records, proof, monkeypatch, tmp_path):
    old_token = proof.token
    monkeypatch.setitem(sso.CONFIG, "audience", AUDIENCE)
    monkeypatch.setitem(sso.CONFIG, "tenant_id", records.plan.tenant_id)
    token = make_gate_record_token(profile_name="test-explicit", region=sso.CONFIG["region"])
    verifier = GateRecordVerifier(sso.verifier(proof))
    actor = verifier(
        Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]})
    )
    snapshot = records.state["runs"]["source"]
    grant = dict(
        subject_id=actor.subject_id,
        tenant_id=actor.tenant_id,
        plan_id=records.plan.plan_id,
        plan_digest=records.plan.plan_digest,
        ref=snapshot.ref,
        digest=snapshot.fingerprint,
        side="source",
        actions=["gate:record", "gate:read"],
        valid_after=(sso.NOW - timedelta(seconds=1)).isoformat(),
        expires_at=(sso.NOW + timedelta(hours=1)).isoformat(),
        administration_ref="reviewed-intake",
    )
    path = tmp_path / "gate-grants.json"
    path.write_text(json.dumps(dict(version=1, grants=[grant])))
    host = GateRecordHost(records.store, GateRecordGrants(path, clock=lambda: proof.clock[0]))
    return SimpleNamespace(
        records=records,
        proof=proof,
        actor=actor,
        token=token,
        old_token=old_token,
        grant=grant,
        path=path,
        host=host,
        app=create_gate_record_app(host, verifier),
        url="/v1/plans/" + records.plan.plan_id + "/runs",
        snapshot=snapshot,
    )


def headers(api):
    return {"Authorization": "Bearer " + api.token}


def body(api):
    return dict(ref=api.snapshot.ref, side="source", document=api.snapshot.document)


def test_record_read_replay_and_revocation(api):
    with TestClient(api.app, base_url="https://host.invalid") as c:
        first = c.post(api.url, json=body(api), headers=headers(api))
        assert first.status_code == 200, first.text
        assert first.json() == {"ref": api.snapshot.ref, "digest": api.snapshot.fingerprint}
        assert c.post(api.url, json=body(api), headers=headers(api)).json() == first.json()
        got = c.get(api.url, params={"ref": api.snapshot.ref}, headers=headers(api))
        assert got.status_code == 200 and got.json()["document"] == api.snapshot.document
        assert got.headers["cache-control"] == "no-store"
        api.path.write_text('{"version":1,"grants":[]}')
        assert (
            c.get(api.url, params={"ref": api.snapshot.ref}, headers=headers(api)).status_code
            == 403
        )
        assert c.post(api.url, json=body(api), headers=headers(api)).status_code == 403
    with api.records.store._transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?", ("gate-run-%",)
            ).fetchone()["n"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?",
                ("gate-current-%",),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize(
    "fault",
    [
        "registration-proof",
        "expired-proof",
        "expired-grant",
        "wrong-plan",
        "wrong-digest",
        "wrong-side",
        "read-only",
        "duplicate",
        "extra-permission",
        "bad-plan-digest",
        "missing",
    ],
)
def test_bad_authority_does_not_write(api, fault):
    h = headers(api)
    grant = dict(api.grant)
    if fault == "registration-proof":
        h["Authorization"] = "Bearer " + api.old_token
    if fault == "expired-proof":
        api.proof.clock[0] += 60
    if fault == "expired-grant":
        grant["expires_at"] = sso.NOW.isoformat()
    if fault == "wrong-plan":
        grant["plan_id"] = "another-plan"
    if fault == "wrong-digest":
        grant["digest"] = "0" * 64
    if fault == "bad-plan-digest":
        grant["plan_digest"] = "0" * 64
    if fault == "wrong-side":
        grant["side"] = "target"
    if fault == "read-only":
        grant["actions"] = ["gate:read"]
    if fault == "extra-permission":
        grant["actions"] = [*grant["actions"], "gate:evaluate"]
    api.path.write_text(
        json.dumps(
            dict(
                version=1,
                grants=[] if fault == "missing" else [grant] * (2 if fault == "duplicate" else 1),
            )
        )
    )
    with TestClient(api.app, base_url="https://host.invalid") as c:
        assert c.post(api.url, json=body(api), headers=h).status_code == 403
    with api.records.store._transaction() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM plan_commands WHERE command_key LIKE ?", ("gate-run-%",)
            ).fetchone()["n"]
            == 0
        )


def test_read_permission_and_record_content_are_independently_bound(api):
    with TestClient(api.app, base_url="https://host.invalid") as c:
        assert c.post(api.url, json=body(api), headers=headers(api)).status_code == 200
        for fault in ("record-only", "digest", "ref", "side"):
            grant = dict(api.grant)
            if fault == "record-only":
                grant["actions"] = ["gate:record"]
            if fault == "digest":
                grant["digest"] = "0" * 64
            if fault == "ref":
                grant["ref"] = "other/ref"
            if fault == "side":
                grant["side"] = "target"
            api.path.write_text(json.dumps(dict(version=1, grants=[grant])))
            assert (
                c.get(api.url, params={"ref": api.snapshot.ref}, headers=headers(api)).status_code
                == 403
            )


def test_tls_bounded_json_and_no_evaluation_or_approval(api):
    with TestClient(api.app, base_url="http://host.invalid") as c:
        assert c.post(api.url, json=body(api), headers=headers(api)).status_code == 403
    with TestClient(api.app, base_url="https://host.invalid") as c:
        for value in (b'{"ref":"a","ref":"b"}', b'{"document":NaN}'):
            assert c.post(api.url, content=value, headers=headers(api)).status_code == 422
        assert c.post(api.url, content=b" " * 1048577, headers=headers(api)).status_code == 413
        for path in ("/grants", "/evaluate", "/v1/plans/p/approvals", "/v1/plans/p/execute"):
            assert c.post(path, json={}, headers=headers(api)).status_code == 404
    assert not isinstance(api.actor, OperatorPrincipal)
