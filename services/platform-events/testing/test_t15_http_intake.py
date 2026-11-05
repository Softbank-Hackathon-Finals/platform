"""T15 signed records through T10 HTTP; cluster/model/identity are fixtures."""

from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from rehearsal_runner.approval_handoff import submit_candidate
from test_t10_recorded_flow import OPERATOR, SPEC, reconcile, start
from test_t10_recorded_flow import flow as flow

from bravo_platform_events.change_intake import Candidate
from bravo_platform_events.change_intake_api import PATH, HttpChangeIntake
from bravo_platform_events.operator_access import create_operator_cutover_app


@pytest.mark.parametrize("lose_ack", [False, True])
def test_signed_t15_result_http_intake_and_single_application_claim(flow, lose_ack):
    deliveries, calls = [], []
    app = create_operator_cutover_app(flow["store"], object(), bindings={"env-aws": "source-ref", "env-onprem": "target-ref"},
        verifier=lambda _request: None, change_intake=flow["intake"])
    with TestClient(app, base_url="https://t10.invalid") as server:
        def transport(request):
            calls.append(request)
            response = server.post(PATH, content=request.content, headers=request.headers)
            assert response.status_code == 200
            if lose_ack:
                raise httpx.ReadTimeout("fixture acknowledgement loss", request=request)
            return httpx.Response(response.status_code, json=response.json())
        client = HttpChangeIntake("https://t10.invalid", transport=httpx.MockTransport(transport))
        def deliver(submission):
            deliveries.append(submit_candidate(submission, candidate_type=Candidate,
                intake=client, token_supplier=lambda: flow["token"]))
        host = flow["service"].host
        flow["service"].host = replace(host, sender=replace(host.sender, deliver=deliver))
        try:
            receipt = start(flow)
            assert len(calls) == 1
            assert deliveries[0].state == ("UNKNOWN" if lose_ack else "ACKNOWLEDGED")
            result = reconcile(flow, receipt)
            assert result["status"] == "PASS"
            with flow["store"]._transaction() as c:
                plan = dict(c.execute("SELECT * FROM plans WHERE plan_id=?", (SPEC.plan_id,)).fetchone())
                current = flow["intake"].current(c, OPERATOR, plan, rehearsal_id=receipt["rehearsal_id"])
            approval = flow["approvals"].approve(OPERATOR, SPEC.plan_id,
                run_id=receipt["rehearsal_id"], binding=current.binding,
                expected_version=1, command_key="approve")
            claim, created = flow["approvals"].claim_application(OPERATOR, SPEC.plan_id,
                approval_id=approval["approval_id"], command_key="apply")
            assert created and claim["verification_id"] == result["verification_id"]
            assert flow["approvals"].claim_application(OPERATOR, SPEC.plan_id,
                approval_id=approval["approval_id"], command_key="apply") == (claim, False)
            assert [event[0] for event in flow["planner"].events] == ["create", "verify", "cleanup"]
            assert len(calls) == 1
        finally:
            client.close()
