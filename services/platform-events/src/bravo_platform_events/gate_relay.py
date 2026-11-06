"""T19 operator relay, run inside the workflow host: what the operator issuer needs from the platform.

One JSON request on stdin, one JSON answer on stdout (operations/gate/run.py sends it through
`kubectl exec`):

    subjects   the exact digests to sign for the plan's current gate context, which purposes are already
               published, and `confirm`: the fingerprint of everything shown, which `sign` must repeat (read only)
    enroll     a public key for one purpose, for the configured issuer subject (gate:manage-provenance)
    readiness  a T13 binding and its snapshot for the current context (gate:record)
    publish    an envelope the operator signed on their own machine (gate:publish-provenance)
    record     a T14 run document for one side (gate:record); the ref is its run_id
    bind       the initial context from the plan's registration and the newest recorded run on each side
               (gate:bind); GateRecords checks it against the stored runs; the same runs again return it
    evaluate   the current context on the production assembly (gate:evaluate); S1 records its BLOCK here,
               the workflow host's GATE stage evaluates the post-apply context itself

Who acts is fixed by the host config /workflow/gate-relay.json, never by the request:

    {"tenant_id": "...", "admin": "<subject>", "issuer": "<subject>", "operator": "<subject>"}

Each subject needs its own OperatorAccess grant for those actions. No private key reaches the platform.
Whoever may `pods/exec` into t10-workflow-host acts as all three subjects: that permission is the whole
gate authority. The three subjects separate grants and records, not the people holding exec.

`readiness` stores the T13 binding and snapshot exactly as sent. Only the operator's readiness-binding and
readiness-snapshot signatures make them trusted; an unsigned newer record blocks the gate until signed.
"""

import hashlib
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

from bravo_gate.contract import GateError, canonical, fingerprint, time
from bravo_gate.models import GateContext, RunSnapshot
from bravo_gate.signatures import AuthorityEnvelope, AuthorityKey

from . import gate_readiness, gate_services
from .gate_records import _current
from .store import EventError

RELAY_CONFIG = "/workflow/gate-relay.json"
OPS = {"subjects", "enroll", "readiness", "publish", "record", "bind", "evaluate"}
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


def subjects(records, principal, plan_id, provenance):
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
        out["confirm"] = fingerprint(out)  # sign repeats this: nothing it did not show gets signed
        purposes = [p for p in ("context", "run-source", "run-target", "readiness-binding", "readiness-snapshot",
                                "gate-result") if p in out]
        out["published"] = [p for p in purposes if provenance._load_envelope(
            view, ctx.fingerprint if p == "context" else out[p], p) is not None]
        return out


def bind_initial(records, principal, plan_id):
    """The initial context: plan registration, the contract catalog and the newest run on each side."""
    with records.store._transaction(plan_id=plan_id) as c:
        plan = records._plan(c, principal, plan_id, "gate:bind")
        spec = json.loads(plan["spec_json"])
        rows = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                         (plan_id, "gate-run-%")).fetchall()
        current = _current(c, plan_id)
    runs = [json.loads(row["receipt_json"]) for row in rows]
    newest = {}
    for side in ("source", "target"):
        mine = [r for r in runs if r.get("side") == side and "ref" in r]
        if not mine:
            raise EventError("gate_run_required")
        newest[side] = max(mine, key=lambda r: time(r["document"]["started_at"]))
    s, t = newest["source"], newest["target"]
    gate_id = "initial-" + hashlib.sha256((s["digest"] + t["digest"]).encode()).hexdigest()[:24]
    ctx = GateContext(plan["tenant_id"], spec["workload_id"], gate_id, spec["plan_digest"], spec["profile_id"],
                      spec["profile_version"], records.contract.catalog_digest, spec["source_environment_id"],
                      s["document"]["environment"]["ref"], spec["target_environment_id"],
                      t["document"]["environment"]["ref"], s["ref"], t["ref"], s["digest"], t["digest"])
    if current is not None and current["context"] == asdict(ctx):
        return current  # the same runs again
    return records.bind(principal, plan_id, gate_id, ctx,
                        expected_revision=current["revision"] if current is not None else 0)


def handle(records, request, config, *, clock=gate_services.server_clock, access=None):
    config = allowed(config)
    if type(request) is not dict or request.get("op") not in OPS or "principal" in request:
        raise ValueError("gate_relay_request_invalid")
    tenant, plan_id, op = config["tenant_id"], request["plan_id"], request["op"]
    provenance = gate_services.provenance(records, clock=clock)
    if op == "subjects":
        return {"subjects": subjects(records, _principal(config["operator"], tenant, clock), plan_id, provenance)}
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
    operator = _principal(config["operator"], tenant, clock)
    if op == "record":
        document = request["document"]
        snapshot = RunSnapshot(document["run_id"], canonical(document))
        return {"recorded": records.record_run(operator, plan_id, request["side"], snapshot)}
    if op == "bind":
        bound = bind_initial(records, operator, plan_id)
        ctx = GateContext(**bound["context"])
        return {"bound": {"gate_id": ctx.gate_id, "context": ctx.fingerprint, "revision": bound["revision"]}}
    if op == "evaluate":
        if access is None:
            raise ValueError("gate_relay_access_required")
        report = records.evaluate(operator, plan_id,
                                  service_factory=gate_services.service_factory(records, access, clock=clock))
        return {"evaluated": {"decision": report["decision"], "context": report["context_digest"],
                              "security": report["security"]["decision"], "data": report["data"]["decision"]}}
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
        access = OperatorAccess(store)
        records = gate_services.gate_records(store, access)
        config = json.loads(Path(RELAY_CONFIG).read_text())
        print(json.dumps({"status": "PASS", **handle(records, request, config, access=access)}, sort_keys=True))
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
