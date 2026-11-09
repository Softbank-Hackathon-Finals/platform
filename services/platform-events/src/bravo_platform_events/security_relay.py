"""Protected kubectl-exec relay for security program registration and revocation."""

import importlib
import json
import re
import sys
from pathlib import Path

from bravo_gate.contract import GateError

from . import gate_services
from .operator_access import OperatorAccess
from .operator_identity import OperatorPrincipal
from .operator_ports import CONFIG
from .postgres import PostgresPlanStore
from .security_governance import SecurityProgramRecords, review_conditions, review_ports
from .store import EventError
from .window_server import runtime_connection

_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _access_clock(clock):
    return lambda: clock().isoformat()


def _principal(config, clock):
    now = int(clock().timestamp())
    registrar = config["security_review"]["registrar"]
    return OperatorPrincipal(registrar["subject_id"], registrar["tenant_id"], now, now + 300, now,
                             "security-registration-relay")


def _ports(store, config, clock):
    module, name = config["gate"].split(":")
    records, load_gate = getattr(importlib.import_module(module), name)(store)
    access = OperatorAccess(store, clock=_access_clock(clock))

    def load_session(connection, principal, plan):
        view = records.view(connection, principal, plan)
        service, readiness = load_gate(view)
        return view.session(service, readiness)

    def authorize(connection, principal, plan, permission):
        access.require(connection, principal, plan, permission)
        return True

    transport, credential, subject, revision = review_ports(config["security_review"], clock=clock)
    programs = SecurityProgramRecords(store, authorize=authorize, load_session=load_session,
                                      baseline_sha=revision, clock=clock)
    return programs, transport, credential, subject


def probe(programs, principal, plan_id, stage, *, transport, credential, subject, clock):
    requirements = {"change": (("patch", "preflight"),),
                    "cutover": (("patch", "postflight"), ("migration", "preflight"))}.get(stage)
    if requirements is None:
        raise ValueError("security_probe_stage_invalid")
    with programs.store._transaction(write=True, plan_id=plan_id) as connection:
        plan = programs._plan(connection, principal, plan_id, "security:read")
        _session, registrations, responses = review_conditions(connection, principal, plan,
            records=programs, requirements=requirements, transport=transport,
            credential_for=credential, subject_id=subject, clock=clock)
    return {"stage": stage, "reviews": [
        {"kind": registration.program.kind.value, "phase": registration.phase.value,
         "registration": registration.fingerprint, "valid_until": response.valid_until.isoformat()}
        for registration, response in zip(registrations, responses, strict=True)]}


def handle(programs, principal, request, *, review=None):
    if (type(request) is not dict or set(request) - {"op", "plan_id", "document", "kind", "phase",
                                                    "expected_revision", "command_key", "stage"}
            or request.get("op") not in {"register", "revoke", "probe"}):
        raise ValueError("security_relay_request_invalid")
    if request["op"] == "probe":
        if set(request) != {"op", "plan_id", "stage"} or not callable(review):
            raise ValueError("security_relay_request_invalid")
        return {"probe": review(request["plan_id"], request["stage"])}
    common = dict(expected_revision=request["expected_revision"], command_key=request["command_key"])
    if request["op"] == "register":
        receipt = programs.register(principal, request["plan_id"], request["document"], **common)
    else:
        receipt = programs.revoke(principal, request["plan_id"], kind=request["kind"],
                                  phase=request["phase"], **common)
    return {"registration": {key: receipt[key] for key in ("fingerprint", "revision", "recorded_at")}}


def main():
    try:
        request = json.loads(sys.stdin.read(1024 * 1024))
        config = json.loads(Path(CONFIG).read_text())
        store = PostgresPlanStore(runtime_connection())
        clock = gate_services.server_clock
        programs, transport, credential, subject = _ports(store, config, clock)
        principal = _principal(config, clock)

        def run_probe(plan_id, stage):
            return probe(programs, principal, plan_id, stage,
                         transport=transport, credential=credential, subject=subject, clock=clock)

        result = handle(programs, principal, request, review=run_probe)
        print(json.dumps({"status": "PASS", **result}, sort_keys=True))
    except (EventError, GateError) as error:
        code = getattr(error, "code", None) or (error.args[0] if error.args else "")
        print(json.dumps({"status": "HOLD", "code": code if isinstance(code, str) and _CODE.fullmatch(code)
                          else type(error).__name__}))
        raise SystemExit(10) from None
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"status": "FAIL", "reason": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
