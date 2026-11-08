"""Run with the T44 API dependency (#628); no substitute approval implementation.

Until that owner PR merges, this module is reported as a dependency skip. It can
also be qualified against its pinned source in an isolated checkout/PYTHONPATH.
The Git repository and verified human sessions below are explicit test fixtures.
"""
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_app import CHANGE, ORIGIN

pytest.importorskip("bravo_platform_events.iac_plans", reason="native T44 IaC API owner PR #628 required")

from bravo_platform_events.cutover import CutoverService  # noqa: E402
from bravo_platform_events.cutover_api import create_cutover_app  # noqa: E402
from bravo_platform_events.iac_plans import IacPlanService  # noqa: E402
from bravo_platform_events.infra_approval import InfraApprovalService  # noqa: E402
from bravo_platform_events.operator_access import OperatorAccess  # noqa: E402
from bravo_platform_events.operator_identity import OperatorPrincipal  # noqa: E402
from bravo_platform_events.plans import PlanSpec, PlanStore  # noqa: E402
from bravo_platform_events.store import EventError  # noqa: E402

from bravo_console import ConsoleApp  # noqa: E402
from bravo_console.delivery import PlanDelivery  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
PARAMS = {"state": "closed", "bucket": "demo-chat-attachments"}


def native_case(tmp_path):
    now = datetime.now(UTC)
    store = PlanStore(tmp_path / "native.db")
    store.register_plan(PlanSpec("plan-iac", "tenant-test", "chat", "env-source", "env-target", "a" * 64, "b" * 64), run_id="registration", occurred_at=now.isoformat())
    access = OperatorAccess(store)
    actors = {name: OperatorPrincipal(name, "tenant-test", int(now.timestamp()), int((now + timedelta(hours=1)).timestamp()), int(now.timestamp()), name + "-session") for name in ("requester", "approver")}
    for name, actor in actors.items():
        access.set_permissions("plan-iac", actor, permissions=["read", "apply_infra", "approve_infra"],
            valid_after=(now - timedelta(seconds=1)).isoformat(), expires_at=(now + timedelta(hours=1)).isoformat(),
            expected_revision=0, administration_ref="fixture-admin", command_key="grant-" + name)

    class Repository:
        opened = 0

        def state(self, environment):
            return copy.deepcopy(json.loads((ROOT / f"infra/iac/environments/{environment}.json").read_text())), "1" * 40

        def open(self, *_a, **_k):
            self.opened += 1
            return dict(number=701, url="https://github.com/Softbank-Hackathon-Finals/platform/pull/701", head_sha="2" * 40)

    repository = Repository()
    approvals = InfraApprovalService(store, check_approve=access.requires("approve_infra"), check_apply=access.requires("apply_infra"), check_access=access.check_read, check_approver=access.requires_subject("approve_infra"))
    iac = IacPlanService(store, repository=repository, infra_service=approvals, check_access=access.check_read, check_request=access.requires("apply_infra"))
    service = CutoverService(store, object(), bindings={"env-source": "registered/source", "env-target": "registered/target"}, check_access=access.check_read)
    def actor(request):
        return actors.get(request.cookies.get("fixture-actor"))
    api = create_cutover_app(service, resolve_principal=actor, infra_service=approvals, iac_service=iac)
    def identity(scope, _plan):
        from starlette.requests import Request
        return actor(Request(scope))
    def authorize(scope, plan, permission):
        try:
            with store._transaction() as c:
                registered = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (plan["plan_id"],)).fetchone())
                access.require(c, identity(scope, plan), registered, permission)
            return True
        except EventError:
            return False
    delivery = PlanDelivery(api, authorize=authorize, subject=lambda s, p: identity(s, p).subject_id,
        modules=[dict(environment_id="env-target", environment="target-dev", module="target-object-exposure", params=PARAMS)])
    return ConsoleApp(api, public_origin=ORIGIN, delivery=delivery), repository


def test_actual_native_plan_other_human_approval_read_claim_and_one_pr(tmp_path):
    app, repository = native_case(tmp_path)
    with TestClient(app, base_url=ORIGIN, cookies={"fixture-actor": "requester"}) as c:
        assert c.get("/v1/plans/plan-iac/delivery").json()["iac"]["configured"] is True
        planned = c.post("/v1/plans/plan-iac/iac/plans", json=dict(environment="target-dev", module="target-object-exposure", params=PARAMS), headers=CHANGE)
        assert planned.status_code == 201, planned.text
        plan = planned.json()
        approval_body = dict(approval_kind="infra", binding=plan["binding"], expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat())
        denied = c.post("/v1/plans/plan-iac/approvals", json=approval_body, headers=CHANGE | {"Idempotency-Key": "self"})
        assert denied.status_code >= 400
        c.cookies.set("fixture-actor", "approver")
        approved = c.post("/v1/plans/plan-iac/approvals", json=approval_body, headers=CHANGE | {"Idempotency-Key": "other"})
        assert approved.status_code == 200, approved.text
        approval = approved.json()
        c.cookies.set("fixture-actor", "requester")
        assert c.get(f"/v1/plans/plan-iac/infra-approvals/{approval['approval_id']}").json()["revoked"] is False
        path = f"/v1/plans/plan-iac/iac/plans/{plan['iac_plan_id']}/pull-request"
        posted = c.post(path, json=dict(approval_id=approval["approval_id"]), headers=CHANGE)
        assert posted.status_code == 201, posted.text
        assert c.post(path, json=dict(approval_id=approval["approval_id"]), headers=CHANGE).json() == posted.json()
        assert repository.opened == 1
        assert c.get(f"/v1/plans/plan-iac/iac/plans/{plan['iac_plan_id']}").json()["pull_request"] == posted.json()
