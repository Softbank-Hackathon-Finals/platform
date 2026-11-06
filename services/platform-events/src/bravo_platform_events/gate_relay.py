"""T19 operator relay, run inside the workflow host: what the operator issuer needs from the platform.

One JSON request on stdin, one JSON answer on stdout (operations/gate/run.py sends it through
`kubectl exec`):

    subjects   the exact digests to sign for the plan's current gate context (read only)
    enroll     a public key for one purpose, for the configured issuer subject (gate:manage-provenance)
    readiness  a T13 binding and its snapshot for the current context (gate:record)
    publish    an envelope the operator signed on their own machine (gate:publish-provenance)

Who acts is fixed by the host config /workflow/gate-relay.json, never by the request:

    {"tenant_id": "...", "admin": "<subject>", "issuer": "<subject>", "operator": "<subject>"}

Each subject needs its own OperatorAccess grant for those actions. No private key reaches the platform.
Exec into the host is still a privileged operator act, but it cannot pick another subject.
"""

import json
import re
import sys
from pathlib import Path

from bravo_gate.contract import GateError, canonical
from bravo_gate.models import RunSnapshot
from bravo_gate.signatures import AuthorityEnvelope, AuthorityKey

from . import gate_readiness, gate_services
from .store import EventError

RELAY_CONFIG = "/workflow/gate-relay.json"
OPS = {"subjects", "enroll", "readiness", "publish"}
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def allowed(config):
    if (type(config) is not dict or set(config) != {"tenant_id", "admin", "issuer", "operator"}
            or not all(isinstance(v, str) and v for v in config.values())
            or len({config["admin"], config["issuer"], config["operator"]}) != 3):
        raise ValueError("gate_relay_configuration_invalid")
    return config


def _principal(subject, tenant, clock, *, lifetime=300):
    from .operator_identity import OperatorPrincipal

    now = int(clock().timestamp())
    return OperatorPrincipal(subject, tenant, now, now + lifetime, now, "gate-relay")


def subjects(records, principal, plan_id):
    """Digests of the current context, both T14 runs, the newest T13 record and the stored report."""
    with records.store._transaction(plan_id=plan_id) as c:
        plan = records._plan(c, principal, plan_id, "gate:read")
        view = records.view(c, principal, plan)
        ctx, actor = view.context, principal.subject_id
        runs = {side: view.run_provider(actor, ctx, side) for side in ("source", "target")}
        binding = gate_readiness.binding_provider(view, actor, ctx)
        report = view.report_provider(actor, ctx)
        out = {"context": {"gate_id": ctx.gate_id, "digest": ctx.fingerprint},
               "run-source": runs["source"].fingerprint, "run-target": runs["target"].fingerprint}
        for side in ("source", "target"):
            doc = runs[side].document
            out[f"run-{side}-window"] = [doc["started_at"], doc["finished_at"]]
        if binding is not None:
            out["readiness-binding"] = binding.fingerprint
            out["readiness-snapshot"] = binding.snapshot_digest
        if report is not None:
            out["gate-result"] = report.fingerprint
        return out


def handle(records, request, config, *, clock=gate_services.server_clock):
    config = allowed(config)
    if type(request) is not dict or request.get("op") not in OPS or "principal" in request:
        raise ValueError("gate_relay_request_invalid")
    tenant, plan_id, op = config["tenant_id"], request["plan_id"], request["op"]
    provenance = gate_services.provenance(records, clock=clock)
    if op == "subjects":
        return {"subjects": subjects(records, _principal(config["operator"], tenant, clock), plan_id)}
    if op == "enroll":
        key = AuthorityKey(request["public_key_hex"], (tenant,), (request["purpose"],),
                           request["valid_from"], request["valid_until"], False)
        record = provenance.enroll(_principal(config["admin"], tenant, clock), plan_id, issuer_id=request["issuer_id"],
                                   key_id=request["key_id"], producer_subject_id=config["issuer"], key=key,
                                   expected_revision=0)
        return {"enrolled": {"issuer_id": request["issuer_id"], "key_id": request["key_id"],
                             "revision": record["revision"]}}
    if op == "readiness":
        binding = gate_readiness.binding_from_dict(request["binding"])
        snapshot = RunSnapshot(request["snapshot"]["ref"], canonical(request["snapshot"]["document"]))
        value = gate_readiness.record_readiness(records, _principal(config["operator"], tenant, clock), plan_id,
                                                binding, snapshot)
        return {"readiness": {"binding": binding.fingerprint, "snapshot": snapshot.fingerprint,
                              "observed_at": value["observed_at"]}}
    envelope = AuthorityEnvelope(**request["envelope"])
    provenance.publish(_principal(config["issuer"], tenant, clock), plan_id, envelope)
    return {"published": {"purpose": envelope.purpose, "subject": envelope.subject_digest}}


def main():
    from .operator_access import OperatorAccess
    from .postgres import PostgresPlanStore
    from .window_server import runtime_connection

    try:
        request = json.loads(sys.stdin.read(1024 * 1024))
        store = PostgresPlanStore(runtime_connection())
        records = gate_services.gate_records(store, OperatorAccess(store))
        config = json.loads(Path(RELAY_CONFIG).read_text())
        print(json.dumps({"status": "PASS", **handle(records, request, config)}, sort_keys=True))
    except (EventError, GateError) as error:
        code = getattr(error, "code", None) or (error.args[0] if error.args else "")
        print(json.dumps({"status": "HOLD", "code": code if isinstance(code, str) and _CODE.fullmatch(code)
                          else type(error).__name__}))
        raise SystemExit(10) from None
    except Exception as error:  # noqa: BLE001 - the operator reads one bounded code, never a trace
        print(json.dumps({"status": "FAIL", "reason": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
