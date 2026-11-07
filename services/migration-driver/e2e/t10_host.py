"""Local T10 for the isolated E2E: the real plan store, grants, approval and claim code on 127.0.0.1.

What is real: PlanStore (SQLite), OperatorAccess per-plan grants, InfraApprovalService (self-approval
refusal, expiry, single claim, revocation), the T10 HTTP routes and OperatorTokenVerifier.
What stands in: the IdP (e2e/idp.py) and the trusted host's administration decision (the grants below
come from this file's config, as a deployment's host configuration would).

usage: python t10_host.py <config.json>
config: {"state_dir", "port", "plan": {PlanSpec fields}, "people": {role: {"sub", "permissions"}}}
"""

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from bravo_platform_events.cutover import CutoverService
from bravo_platform_events.cutover_api import create_cutover_app
from bravo_platform_events.infra_approval import InfraApprovalService
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.operator_identity import OperatorPrincipal, OperatorTokenVerifier, operator_id
from bravo_platform_events.plans import PlanSpec, PlanStore
from idp import ISSUER, LocalIdp


def now_iso():
    return datetime.now(UTC).isoformat()


def build(config: dict):
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    idp = LocalIdp(state)
    spec = PlanSpec(**config["plan"])
    store = PlanStore(state / "t10.db")
    store.register_plan(spec, run_id="e2e-register", occurred_at=now_iso())  # same spec again is a no-op

    access = OperatorAccess(store, clock=now_iso)
    # One grant window per state directory: a restart replays the same administrative command.
    window_file = state / "grant-window.json"
    if not window_file.exists():
        window_file.write_text(json.dumps({"valid_after": now_iso(),
                                           "expires_at": (datetime.now(UTC) + timedelta(hours=12)).isoformat()}))
    window = json.loads(window_file.read_text())
    for role, person in config["people"].items():
        now = int(time.time())
        principal = OperatorPrincipal(operator_id(ISSUER, person["sub"]), spec.tenant_id, now, now + 300, now, "e2e")
        access.set_permissions(spec.plan_id, principal, permissions=person["permissions"], expected_revision=0,
                               administration_ref="e2e-host-config", command_key=f"grant-{role}", **window)
    verifier = OperatorTokenVerifier(**idp.verifier_config({p["sub"]: spec.tenant_id for p in config["people"].values()}))
    infra = InfraApprovalService(store, check_approve=access.requires("approve_infra"),
                                 check_apply=access.requires("apply_infra"), check_access=access.check_read,
                                 check_approver=access.requires_subject("approve_infra"), clock=now_iso)
    bindings = {spec.source_environment_id: "cutover/source", spec.target_environment_id: "cutover/target"}
    cutover = CutoverService(store, None, bindings=bindings, check_access=access.check_read, clock=now_iso)
    return create_cutover_app(cutover, resolve_principal=access.resolver(verifier), infra_service=infra)


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    uvicorn.run(build(config), host="127.0.0.1", port=int(config["port"]), log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
