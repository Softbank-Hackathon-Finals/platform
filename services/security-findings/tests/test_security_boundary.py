"""Security regressions use synthetic data and local SQLite only."""

import copy
import hashlib
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import READ_AUTH, READER_TOKEN, WRITE_AUTH, WRITER_TOKEN, credential
from fastapi.testclient import TestClient

from bravo_findings import Normalizer, Rules
from bravo_findings.api import create_app
from bravo_findings.auth import Authenticator, AuthUnavailable
from bravo_findings.collectors import run_once
from bravo_findings.ingest import Ingestor
from bravo_findings.rules import rules_dir
from bravo_findings.store import EventConflict, Store
from bravo_findings.view import View

NOW = 1_793_786_400_000
SECRET = "SYNTHETIC_REVIEW_VALUE_NOT_A_REAL_CREDENTIAL"


def raw(name):
    return json.loads((rules_dir() / "examples" / f"{name}.raw.json").read_text())


@pytest.fixture
def context(service_credentials):
    store = Store()
    now = [NOW]
    records = service_credentials(NOW)
    app = create_app(store=store, credentials=records, clock=lambda: now[0])
    return store, now, records, TestClient(app)


def send(client, event, source="kyverno", headers=WRITE_AUTH, environment="env-onprem"):
    return client.post(
        f"/v1/security/ingest/{source}",
        headers=headers,
        json={"environment_id": environment, "events": [event]},
    )


@pytest.mark.parametrize("route", ["summary", "coverage", "sources", "findings"])
def test_reads_require_credentials_and_scope(context, route):
    store, _, _, client = context
    for env in ("env-onprem", "other-org:prod"):
        store.register_source("falco", env)
    assert client.get(f"/v1/security/{route}").status_code == 401
    assert client.get(f"/v1/security/{route}", headers=WRITE_AUTH).status_code == 403
    response = client.get(f"/v1/security/{route}", headers=READ_AUTH)
    assert response.status_code == 200
    assert "other-org" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert (
        client.get(
            f"/v1/security/{route}", headers=READ_AUTH, params={"environment_id": "other-org:prod"}
        ).status_code
        == 403
    )


def test_scope_is_applied_before_pagination(context):
    store, _, _, client = context
    n = Normalizer()
    for env in ("env-onprem", "other-org:prod"):
        event = raw("onprem-kyverno-fail")
        if env != "env-onprem":
            event["severity"] = "critical"
        store.upsert(n.normalize("kyverno", event, environment_id=env, collected_time=NOW))
    result = client.get("/v1/security/findings?limit=1", headers=READ_AUTH).json()
    assert len(result) == 1 and result[0]["bravo"]["environment_id"] == "env-onprem"


def test_write_requires_source_and_environment_and_read_is_not_write(context):
    store, _, records, client = context
    event = raw("onprem-kyverno-fail")
    assert send(client, event, headers=READ_AUTH).status_code == 403
    assert send(client, event, environment="other-org").status_code == 403
    records[1]["sources"] = ["falco"]
    assert send(client, event).status_code == 403
    assert store.findings() == [] and store.sources() == []
    assert store._db.execute("SELECT count(*) FROM audit WHERE outcome='denied'").fetchone()[0] > 0


def test_controls_cannot_claim_another_environment(context):
    store, _, _, client = context
    event = raw("controls-iam-01-1-fail")
    event["environment"]["id"] = "other-org"
    result = send(client, event, "bravo_controls").json()
    assert result["recorded"] == "failure"
    assert store.findings(open_only=False) == []


def test_expired_not_yet_valid_revoked_and_malformed_bearer(context):
    store, now, records, client = context
    for authorization in (
        "",
        "Basic " + READER_TOKEN,
        READER_TOKEN,
        "Bearer short",
        "Bearer " + SECRET,
    ):
        assert (
            client.get("/v1/security/summary", headers={"Authorization": authorization}).status_code
            == 401
        )
    records[0]["revoked"] = True
    assert client.get("/v1/security/summary", headers=READ_AUTH).status_code == 401
    records[0]["revoked"] = False
    now[0] = records[0]["not_before"] - 1
    assert client.get("/v1/security/summary", headers=READ_AUTH).status_code == 401
    now[0] = records[0]["expires_at"]
    assert client.get("/v1/security/summary", headers=READ_AUTH).status_code == 401
    assert store.sources() == []
    audit = str([tuple(r) for r in store._db.execute("SELECT * FROM audit")])
    assert READER_TOKEN not in audit and SECRET not in audit


def test_auth_file_rotation_missing_bad_permissions_and_corruption_fail_closed(tmp_path):
    path = tmp_path / "auth.json"
    record = credential(READER_TOKEN, NOW)
    path.write_text(json.dumps({"version": 1, "credentials": [record]}))
    path.chmod(0o600)
    auth = Authenticator(path=str(path))
    assert auth.authenticate(READ_AUTH["Authorization"], NOW).subject == "tester"
    record["revoked"] = True
    replacement = tmp_path / "replacement"
    replacement.write_text(json.dumps({"version": 1, "credentials": [record]}))
    replacement.chmod(0o600)
    replacement.replace(path)
    assert auth.authenticate(READ_AUTH["Authorization"], NOW) is None
    path.chmod(0o644)
    with pytest.raises(AuthUnavailable):
        auth.authenticate(READ_AUTH["Authorization"], NOW)
    path.chmod(0o600)
    path.write_text(SECRET)
    with pytest.raises(AuthUnavailable) as error:
        auth.authenticate(READ_AUTH["Authorization"], NOW)
    assert SECRET not in str(error.value)
    path.unlink()
    with pytest.raises(AuthUnavailable):
        auth.authenticate(READ_AUTH["Authorization"], NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"environments": ["*"]},
        {"permissions": ["admin"]},
        {"permissions": ["read", "ingest"], "sources": ["falco"]},
        {"permissions": ["read"], "sources": ["falco"]},
        {"permissions": ["ingest"], "sources": []},
        {"expires_at": NOW + 86_400_001},
        {"extra": True},
        {"token_sha256": "x"},
    ],
)
def test_invalid_credential_configuration_rejected(change):
    record = {**credential(READER_TOKEN, NOW), **change}
    with pytest.raises(ValueError):
        Authenticator([record])


def test_duplicates_and_symlinks_are_not_trusted(tmp_path):
    record = credential(READER_TOKEN, NOW)
    with pytest.raises(ValueError):
        Authenticator([record, dict(record)])
    real = tmp_path / "real"
    real.write_text(json.dumps({"version": 1, "credentials": [record]}))
    real.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(AuthUnavailable):
        Authenticator(path=str(link)).authenticate(READ_AUTH["Authorization"], NOW)


def test_silence_while_failed_or_healthy_never_closes_finding(context):
    store, now, _, client = context
    send(client, raw("onprem-falco-shell"), "falco")
    assert client.post("/v1/security/maintenance/resolve-silent").status_code == 401
    assert (
        client.post("/v1/security/maintenance/resolve-silent", headers=READ_AUTH).status_code == 410
    )
    for failure in (False, True):
        if failure:
            store.record_failure("falco", "env-onprem", NOW, "SOURCE_UNHEALTHY")
        now[0] = NOW + 25 * 3_600_000
        with pytest.raises(ValueError, match="SILENCE_IS_NOT_RESOLUTION"):
            store.resolve_silent("falco", now[0])
        assert len(store.findings()) == 1


def test_quarantine_is_unknown_and_heartbeat_does_not_clear_it(context):
    store, _, _, client = context
    result = send(client, {"result": "fail"}).json()
    assert result == {"recorded": "failure", "quarantined": 1, "error_code": "INGESTION_GAP"}
    client.post(
        "/v1/security/ingest/kyverno",
        headers=WRITE_AUTH,
        json={"environment_id": "env-onprem", "events": []},
    )
    result = send(client, raw("onprem-kyverno-fail")).json()
    assert result["created"] == 1 and result["recorded"] == "failure"
    cell = client.get("/v1/security/summary", headers=READ_AUTH).json()["environments"][0]["cells"][
        "POLICY"
    ]
    assert cell["state"] == "UNKNOWN" and cell["counts"] == {"3": 1}


def test_pull_quarantine_never_advances_cursor_even_after_restart(tmp_path):
    path = str(tmp_path / "findings.db")
    store = Store(path)
    store.register_source("aws_security_hub", "env-aws")
    store.record_success("aws_security_hub", "env-aws", NOW - 1000, cursor=str(NOW - 1000))

    class Pull:
        source = "aws_security_hub"

        def fetch(self, since, until):
            yield {"Title": "broken"}

    result = run_once(Ingestor(store, Normalizer()), Pull(), "env-aws", lambda: NOW)
    assert result == {"quarantined": 1, "failed": 1}
    restored = Store(path)
    restored.record_success("aws_security_hub", "env-aws", NOW + 1, cursor=str(NOW + 1))
    assert restored.source("aws_security_hub", "env-aws").cursor == str(NOW - 1000)
    assert View(restored, Rules()).statuses(NOW)[0]["state"] == "FAILED"


def test_old_pass_cannot_replace_new_fail_and_equal_time_conflict_is_held(context):
    store, _, _, client = context
    event = raw("controls-iam-01-1-fail")
    assert send(client, event, "bravo_controls").json()["created"] == 1
    older = copy.deepcopy(event)
    older["result"].update(verdict="PASS", observed="deny")
    older["result"]["evidence"]["collected_at"] = "2026-11-04T09:00:00Z"
    assert send(client, older, "bravo_controls").json()["stale"] == 1
    assert len(store.findings()) == 1
    older["result"]["evidence"]["collected_at"] = event["result"]["evidence"]["collected_at"]
    assert send(client, older, "bravo_controls").json()["error_code"] == "INGESTION_GAP"
    assert len(store.findings()) == 1


def test_valid_newer_pass_and_duplicate_are_idempotent(context):
    store, _, _, client = context
    event = raw("controls-iam-01-1-fail")
    send(client, event, "bravo_controls")
    event["result"].update(verdict="PASS", observed="deny")
    event["result"]["evidence"]["collected_at"] = "2026-11-04T09:02:00Z"
    assert send(client, event, "bravo_controls").json()["closed"] == 1
    assert send(client, event, "bravo_controls").json()["duplicate"] == 1
    assert store.findings() == []
    assert len(store.findings(open_only=False)) == 1


@pytest.mark.parametrize("stamp", ["2099-11-04T09:00:00Z", "2026-11-04T09:00:00", True, -1])
def test_invalid_source_time_is_quarantined(context, stamp):
    store, _, _, client = context
    event = raw("onprem-falco-shell")
    event["time"] = stamp
    assert send(client, event, "falco").json()["quarantined"] == 1
    assert store.findings() == []


def test_lossy_mask_does_not_merge_different_accounts():
    store, n = Store(), Normalizer()
    for account in ("123456789012", "999999999012"):
        event = raw("aws-security-hub-control")
        event["Id"] = f"arn:aws:securityhub:ap-northeast-1:{account}:finding/a"
        store.upsert(
            n.normalize("aws_security_hub", event, environment_id="env-aws", collected_time=NOW)
        )
    assert len(store.findings()) == 2


def test_no_sensitive_raw_or_validation_input_in_storage_response_logs(context, caplog):
    store, _, _, client = context
    event = raw("onprem-falco-shell")
    event.update(
        output=f"curl -H Authorization:Bearer {SECRET}",
        password=SECRET,
        unexpected={"secret": SECRET},
    )
    event["output_fields"].update({"proc.cmdline": SECRET, "password": SECRET})
    assert send(client, event, "falco").json()["created"] == 1
    invalid = send(client, {"result": "fail", "policy": {"password": SECRET}})
    invalid_shape = client.post(
        "/v1/security/ingest/falco",
        headers=WRITE_AUTH,
        json={"environment_id": SECRET, "events": SECRET},
    )
    response = client.get("/v1/security/findings", headers=READ_AUTH)
    db = "\n".join(store._db.iterdump())
    for output in (invalid.text, invalid_shape.text, response.text, db, caplog.text):
        assert SECRET not in output
    assert client.get("/v1/security/findings").status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        '{"events":[],"events":[]}',
        "[" * 40 + "0" + "]" * 40,
        '{"value": NaN}',
        '{"value": Infinity}',
    ],
)
def test_untrusted_json_is_rejected_before_mutation(context, body):
    store, _, _, client = context
    response = client.post(
        "/v1/security/ingest/falco",
        content=body,
        headers={**WRITE_AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert store.sources() == []


def test_body_limit_does_not_trust_content_length_and_auth_runs_first(context):
    store, _, _, client = context
    body = '"' + "x" * 1_048_576 + '"'
    assert client.post("/v1/security/ingest/falco", content=body).status_code == 401
    response = client.post(
        "/v1/security/ingest/falco",
        content=body,
        headers={**WRITE_AUTH, "Content-Type": "application/json", "Content-Length": "1"},
    )
    assert response.status_code == 413
    assert store.sources() == []


def test_no_implicit_memory_storage_and_no_legacy_auth(monkeypatch):
    monkeypatch.delenv("BRAVO_FINDINGS_DB", raising=False)
    with pytest.raises(ValueError, match="must persist"):
        create_app()
    monkeypatch.setenv("BRAVO_INGEST_TOKENS", '{"e":"legacy"}')
    with pytest.raises(ValueError, match="legacy ingest"):
        create_app(store=Store())


def test_legacy_database_is_not_silently_reused_or_deleted(tmp_path):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE findings (unsafe TEXT)")
    db.execute("INSERT INTO findings VALUES (?)", (SECRET,))
    db.commit()
    db.close()
    os.chmod(path, 0o600)
    with pytest.raises(ValueError, match="reviewed export"):
        Store(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT unsafe FROM findings").fetchone()[0] == SECRET


def test_batch_storage_failure_rolls_back_findings_status_and_audit(context):
    store, _, _, client = context
    store._db.execute(
        "CREATE TRIGGER fail_audit BEFORE INSERT ON audit "
        "WHEN NEW.action='ingest' BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    assert send(client, raw("onprem-kyverno-fail")).status_code == 503
    assert store.findings(open_only=False) == []
    assert store.sources() == []
    assert store._db.execute("SELECT count(*) FROM finding_history").fetchone()[0] == 0


def test_two_connections_cannot_overwrite_new_event_with_old_event(tmp_path):
    path = str(tmp_path / "concurrent.db")
    stores = [Store(path), Store(path)]
    n = Normalizer()
    event = raw("controls-iam-01-1-fail")
    newer = n.normalize("bravo_controls", event, environment_id="env-onprem", collected_time=NOW)
    older = copy.deepcopy(newer)
    older.update(time=newer["time"] - 1000, status_id=4)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda pair: pair[0].upsert(pair[1]), zip(stores, (newer, older), strict=True))
        )
    assert "created" in results or "updated" in results
    assert stores[0].findings()[0]["time"] == newer["time"]
    same_time = copy.deepcopy(newer)
    same_time["status_id"] = 4
    with pytest.raises(EventConflict):
        stores[1].upsert(same_time)


def test_gap_survives_a_source_error_in_the_same_batch(context):
    store, _, _, client = context
    response = client.post(
        "/v1/security/ingest/kyverno",
        headers=WRITE_AUTH,
        json={
            "environment_id": "env-onprem",
            "events": [{"result": "fail"}, {"result": "error", "policy": "p"}],
        },
    )
    assert response.json()["recorded"] == "failure"
    assert store._db.execute("SELECT count(*) FROM quarantine").fetchone()[0] == 1
    response = client.post(
        "/v1/security/ingest/kyverno",
        headers=WRITE_AUTH,
        json={"environment_id": "env-onprem", "events": []},
    )
    assert response.json()["error_code"] == "INGESTION_GAP"


def test_first_pull_covers_old_open_findings_and_incremental_overlap():
    store = Store()
    windows = []

    class Pull:
        source = "aws_security_hub"

        def fetch(self, since, until):
            windows.append((since, until))
            event = raw("aws-security-hub-control")
            event["UpdatedAt"] = "2025-01-01T00:00:00Z"
            yield event

    ingestor = Ingestor(store, Normalizer())
    assert run_once(ingestor, Pull(), "env-aws", lambda: NOW)["created"] == 1
    assert windows[0] == (0, NOW)
    assert run_once(ingestor, Pull(), "env-aws", lambda: NOW + 1000)["duplicate"] == 1
    assert windows[1][0] == NOW - 300_000


def test_disabled_source_is_not_reenabled_by_sender_or_puller(context):
    store, _, _, client = context
    store.register_source("falco", "env-onprem", enabled=False)
    assert send(client, raw("onprem-falco-shell"), "falco").status_code == 409
    assert store.source("falco", "env-onprem").enabled is False

    class Pull:
        source = "falco"

        def fetch(self, since, until):
            raise AssertionError("disabled source must not be read")

    assert run_once(Ingestor(store, Normalizer()), Pull(), "env-onprem", lambda: NOW) == {
        "disabled": 1
    }


def test_source_status_cannot_move_backwards_or_claim_future_freshness():
    store = Store()
    store.register_source("falco", "env-onprem")
    store.record_failure("falco", "env-onprem", NOW, "SOURCE_UNHEALTHY")
    store.record_success("falco", "env-onprem", NOW - 1)
    assert View(store, Rules()).statuses(NOW)[0]["state"] == "FAILED"
    store.record_success("falco", "env-onprem", NOW + 60_000)
    assert View(store, Rules()).statuses(NOW)[0]["state"] == "STALE"


def test_retained_projection_is_minimal_and_hash_verifiable(context):
    store, _, _, client = context
    event = raw("onprem-falco-shell")
    event["output"] = SECRET
    send(client, event, "falco")
    row = store._db.execute("SELECT raw_sha256,raw FROM raw").fetchone()
    projection = json.loads(row["raw"])
    assert "output" not in projection and "hostname" not in projection
    encoded = json.dumps(
        projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert row["raw_sha256"] == "sha256:" + hashlib.sha256(encoded).hexdigest()


def test_duplicate_authorization_headers_are_rejected(context):
    store, _, _, client = context
    headers = [
        ("Authorization", "Bearer " + READER_TOKEN),
        ("Authorization", "Bearer " + WRITER_TOKEN),
    ]
    assert client.get("/v1/security/summary", headers=headers).status_code == 401
    assert store.sources() == []


def test_slow_body_has_total_time_budget(monkeypatch):
    import asyncio

    import bravo_findings.boundary as boundary

    monkeypatch.setattr(boundary, "BODY_TIMEOUT_SECONDS", 0.001)
    app = create_app(store=Store(), credentials=[credential(READER_TOKEN, NOW)], clock=lambda: NOW)
    sent = []

    async def receive():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b""}

    async def send_message(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/security/summary",
        "headers": [(b"authorization", READ_AUTH["Authorization"].encode())],
        "query_string": b"",
        "scheme": "http",
    }
    asyncio.run(app(scope, receive, send_message))
    assert sent[0]["status"] == 408


def test_unknown_database_version_is_not_silently_downgraded(tmp_path):
    path = str(tmp_path / "future.db")
    store = Store(path)
    store._db.execute("UPDATE security_schema SET version=99")
    with pytest.raises(ValueError, match="unsupported findings database"):
        Store(path)
    assert store._db.execute("SELECT version FROM security_schema").fetchone()[0] == 99


def test_persistent_database_permissions_fail_closed(tmp_path):
    path = tmp_path / "state.db"
    Store(str(path))
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        Store(str(path))
    path.chmod(0o600)
    link = tmp_path / "linked.db"
    link.symlink_to(path)
    with pytest.raises(OSError):
        Store(str(link))
