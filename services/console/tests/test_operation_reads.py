"""Native T10 actions, actual durable receipts; no real cluster mutations."""

from datetime import UTC, datetime, timedelta

from expanded_fixture import setup_expanded
from fastapi.testclient import TestClient
from test_app import CHANGE, ORIGIN


def client(case):
    return TestClient(case[0], base_url=ORIGIN, cookies={"fixture-session": "test-only"})


def test_shift_uses_native_t10_and_receipt_lookup_never_submits(tmp_path):
    case = setup_expanded(tmp_path / "state.db")
    path = "/v1/plans/plan-expanded"
    body = {
        "approval_kind": "shift",
        "expected_version": 3,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    headers = CHANGE | {"Idempotency-Key": "shift-one"}
    with client(case) as c:
        before = case[1].snapshot("plan-expanded")
        response = c.post(path + "/approvals", json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["approval_kind"] == "shift"
        for _ in range(2):
            lookup = c.get(path + "/operation-receipts/shift-one")
            assert lookup.status_code == 200 and lookup.json()["operation"] == "shift"
            assert lookup.json()["receipt"]["approval_id"] == response.json()["approval_id"]
        assert case[1].snapshot("plan-expanded") == before
        assert case[2]["failback_posts"] == 0
        case[2]["actor"] = "other-operator"
        assert c.get(path + "/operation-receipts/shift-one").status_code == 403


def test_native_rehearsal_is_single_submit_and_readable_after_lost_response(tmp_path):
    case = setup_expanded(tmp_path / "state.db", phase="REHEARSE")
    path = "/v1/plans/plan-expanded"
    body = {"task_id": "task-registered", "expected_version": 3, "attempt": 1}
    headers = CHANGE | {"Idempotency-Key": "rehearsal-one"}
    with client(case) as c:
        first = c.post(path + "/rehearsals", json=body, headers=headers)
        assert first.status_code == 202, first.text
        assert c.post(path + "/rehearsals", json=body, headers=headers).json() == first.json()
        lookup = c.get(path + "/operation-receipts/rehearsal-one").json()
        assert lookup["receipt"]["rehearsal_id"] == first.json()["rehearsal_id"]
        assert case[2]["starts"] == 1
        result = c.post(
            f"{path}/rehearsals/{first.json()['rehearsal_id']}/attempts/1/reconcile",
            json={},
            headers=CHANGE,
        )
        assert result.status_code == 200 and result.json()["status"] == "UNCONFIRMED"
        assert case[2]["starts"] == 1
        context = c.get(path + "/workspace").json()["operations"]
        assert context["rehearsal_runs"][0]["rehearsal_id"] == first.json()["rehearsal_id"]


def test_failback_binding_is_server_checked_and_submit_is_separate_and_single(tmp_path):
    case = setup_expanded(tmp_path / "state.db", phase="WATCH")
    path = "/v1/plans/plan-expanded"
    with client(case) as c:
        context = c.get(path + "/workspace").json()["operations"]
        assert context["actions"]["failback-approve"] is True
        binding = context["failback_binding"]
        assert binding["source_environment_id"] == "env-target"
        approval = c.post(
            path + "/approvals",
            json={"approval_kind": "failback", "expected_version": 3, "binding": binding},
            headers=CHANGE | {"Idempotency-Key": "fb-approval"},
        )
        assert approval.status_code == 200, approval.text
        assert case[2]["failback_posts"] == 0
        response = c.post(
            path + "/failback",
            json={"approval_id": approval.json()["approval_id"]},
            headers=CHANGE | {"Idempotency-Key": "fb-submit"},
        )
        assert response.status_code == 202, response.text
        assert case[2]["failback_posts"] == 1
        command = c.get(path + "/operation-receipts/fb-submit")
        assert (
            command.status_code == 200
            and command.json()["receipt"]["command_id"] == response.json()["command_id"]
        )
        assert "connection_ref" not in command.text
        assert case[2]["failback_posts"] == 1


def test_initial_operator_is_durably_claimed_and_lookup_and_same_key_do_not_repeat(tmp_path):
    case = setup_expanded(tmp_path / "state.db", phase="PROPOSE")
    path = "/v1/plans/plan-expanded"
    body = {"expected_version": 3, "plan_digest": case[1].snapshot("plan-expanded")["plan_digest"]}
    with client(case) as c:
        first = c.post(path + "/initial-proposal/start", json=body, headers=CHANGE)
        assert first.status_code == 200 and first.json()["status"] == "DONE", first.text
        assert (
            c.post(path + "/initial-proposal/start", json=body, headers=CHANGE).json()
            == first.json()
        )
        assert (
            c.get(path + "/operation-receipts/command-one").json()["operation"] == "initial-start"
        )
        assert case[2]["initial"] == 1
        assert (
            c.post(path + "/initial-proposal/collect", json=body, headers=CHANGE).status_code == 409
        )
        case[2]["grants"] = False
        assert c.get(path + "/operation-receipts/command-one").status_code == 403


def test_unknown_initial_result_never_automatically_dispatches_again(tmp_path):
    case = setup_expanded(tmp_path / "state.db", phase="PROPOSE")

    def unknown(*_):
        case[2]["initial"] += 1
        raise RuntimeError("PRIVATE_INTERNAL_ERROR")

    case[0].workspace.operations.initial = unknown
    path = "/v1/plans/plan-expanded"
    body = {"expected_version": 3, "plan_digest": case[1].snapshot("plan-expanded")["plan_digest"]}
    with client(case) as c:
        assert c.post(path + "/initial-proposal/start", json=body, headers=CHANGE).json() == {
            "status": "UNKNOWN"
        }
        assert c.post(path + "/initial-proposal/start", json=body, headers=CHANGE).json() == {
            "status": "UNKNOWN"
        }
        result = c.get(path + "/operation-receipts/command-one")
        assert result.json()["receipt"]["status"] == "UNKNOWN" and "PRIVATE" not in result.text
        assert case[2]["initial"] == 1


def test_initial_uses_current_transaction_permission_and_rejects_client_paths(tmp_path):
    case = setup_expanded(tmp_path / "state.db", phase="PROPOSE")
    case[0].workspace.operations.check_initial = lambda *_: False
    path = "/v1/plans/plan-expanded/initial-proposal/start"
    body = {"expected_version": 3, "plan_digest": case[1].snapshot("plan-expanded")["plan_digest"]}
    with client(case) as c:
        assert c.post(path, json=body, headers=CHANGE).status_code == 403
        assert case[2]["initial"] == 0
        case[0].workspace.operations.check_initial = lambda *_: True
        assert (
            c.post(path, json=body | {"command": "not-allowed"}, headers=CHANGE).status_code == 400
        )
        assert (
            c.post(path, json=body, headers={"Idempotency-Key": "command-one"}).status_code == 403
        )
        assert case[2]["initial"] == 0


def test_initial_wait_does_not_return_data_after_session_revocation(tmp_path):
    case = setup_expanded(tmp_path / "state.db", phase="PROPOSE")

    def revoke(*_):
        case[2]["allowed"] = False
        return {"status": "DONE", "request_id": "must-not-leak"}

    case[0].workspace.operations.initial = revoke
    body = {"expected_version": 3, "plan_digest": case[1].snapshot("plan-expanded")["plan_digest"]}
    with client(case) as c:
        response = c.post(
            "/v1/plans/plan-expanded/initial-proposal/start", json=body, headers=CHANGE
        )
    assert response.status_code == 403 and "must-not-leak" not in response.text


def test_duplicate_idempotency_header_and_ambiguous_json_cannot_dispatch(tmp_path):
    case = setup_expanded(tmp_path / "state.db", phase="PROPOSE")
    path = "/v1/plans/plan-expanded/initial-proposal/start"
    with client(case) as c:
        headers = [(k, v) for k, v in CHANGE.items()] + [("Idempotency-Key", "second-key")]
        assert c.post(path, json={}, headers=headers).status_code == 400
        assert (
            c.post(
                path, content=b'{"expected_version":3,"expected_version":4}', headers=CHANGE
            ).status_code
            == 400
        )
        assert c.post(path, content=b"\xff", headers=CHANGE).status_code == 400
    assert case[2]["initial"] == 0
