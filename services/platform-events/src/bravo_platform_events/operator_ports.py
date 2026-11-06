"""Production ports for the operator approval host (`approval_server --ports ...operator_ports:create`).

The approval API (change, SHIFT and cutover approvals, failback) runs only through
`trusted_rehearsal.build_trusted_host`, which needs TrustedHostPorts. One protected JSON names mounted
files only (`/rehearsal/operator.json`):

    {"executor": {"url": "https://cutover-executor.bravo-platform.svc:8080", "ca_file": "/executor/ca.crt",
                  "token_file": "/executor/read-token", "command_token_file": "/executor/command-token",
                  "registered_pair": {...}},
     "gate": "module:factory",              # T19: factory(store) -> (GateRecords, load_gate)
     "failback": "module:factory",          # optional T20: factory(store) -> (authority, read_integrity)
     "write_pause_budget_ms": 30000}

Cutover command policy (current_cutover):
- binding: the change T31 applied (the single consumed change claim: diff_hash, image_digest), the
  plan's current GATE context (gate_id, context_digest) and, as expiry, the human SHIFT approval's
  expiry. Every value is a stored record, so approval and submission see the same context.
- command: epoch = the plan version the cutover is approved at (used once; CutoverService refuses a
  reused epoch), the registered source/target connection refs, the configured write-pause budget and
  deadline = the binding expiry.
"""

import hashlib
import json
from pathlib import Path

from .cutover import CurrentCutover
from .store import EventError

CONFIG = "/rehearsal/operator.json"


def _shift_approval(c, plan):
    for version in range(plan["version"], 0, -1):
        row = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key=?",
                        (plan["plan_id"], f"shift-approval-{version}")).fetchone()
        if row is not None:
            return json.loads(row["receipt_json"])
    return None


def current_cutover(bindings, *, write_pause_budget_ms=30000):
    """Upstream CurrentCutover from stored records only (no client input, no network)."""
    from bravo_gate.models import GateContext

    from .gate_records import _current

    if not isinstance(bindings, dict) or len(bindings) != 2 or type(write_pause_budget_ms) is not int:
        raise ValueError("cutover_policy_configuration_invalid")

    def current(c, principal, plan, *, now=None, approved_by=None, approval_id=None, **_selection):
        spec = json.loads(plan["spec_json"])
        claims = [json.loads(r["receipt_json"]) for r in c.execute(
            "SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
            (plan["plan_id"], "change-consumed-%")).fetchall()]
        if len(claims) != 1:
            raise EventError("authority_unavailable")  # none applied yet, or ambiguous
        change = claims[0]["binding"]
        gate = _current(c, plan["plan_id"])
        shift = _shift_approval(c, plan)
        if gate is None or shift is None:
            raise EventError("authority_unavailable")
        context = GateContext(**gate["context"])
        expires = shift["expires_at"]
        binding = dict(plan_id=plan["plan_id"], plan_digest=spec["plan_digest"],
                       diff_hash=change["diff_hash"], image_digest=change["image_digest"],
                       target_environment_id=spec["target_environment_id"], gate_id=context.gate_id,
                       context_digest=context.fingerprint, expires_at=expires)
        command = dict(plan_id=plan["plan_id"], epoch=plan["version"], write_pause_budget_ms=write_pause_budget_ms,
                       deadline=expires)
        for side in ("source", "target"):
            env = spec[side + "_environment_id"]
            if env not in bindings:
                raise EventError("authority_unavailable")
            command[side] = dict(environment_id=env, connection_ref=bindings[env])
        revision = "operations-" + hashlib.sha256(json.dumps([binding, command], sort_keys=True).encode()).hexdigest()[:32]
        return CurrentCutover(binding, command, revision)

    return current


def create(store, config_file=CONFIG):
    import importlib
    import ssl

    from .credential_host import create_ports as rehearsal_ports
    from .executor import ExecutorClient
    from .trusted_rehearsal import TrustedHostPorts

    config = json.loads(Path(config_file).read_text())
    executor = config.get("executor") if isinstance(config, dict) else None
    if (type(config) is not dict or not {"executor", "gate"} <= set(config) <= {"executor", "gate", "write_pause_budget_ms", "failback"}
            or type(executor) is not dict
            or set(executor) != {"url", "ca_file", "token_file", "command_token_file", "registered_pair"}
            or not executor["url"].startswith("https://")):
        raise ValueError("operator_ports_configuration_invalid")
    read = Path(executor["token_file"]).read_text().strip()
    command = Path(executor["command_token_file"]).read_text().strip()
    client = ExecutorClient(executor["url"], token=read, command_token=command,
                            tls_context=ssl.create_default_context(cafile=executor["ca_file"]))
    bindings = client.verified_bindings(executor["registered_pair"])
    module, name = config["gate"].split(":")
    records, load_gate = getattr(importlib.import_module(module), name)(store)
    rehearsal = rehearsal_ports(store)
    authority = records.cutover_authority(
        upstream=current_cutover(bindings, write_pause_budget_ms=config.get("write_pause_budget_ms", 30000)),
        load_gate=load_gate)
    failback = {}
    if "failback" in config:
        module, name = config["failback"].split(":")
        upstream, read_integrity = getattr(importlib.import_module(module), name)(store)
        failback = {"failback_authority": upstream, "read_integrity": read_integrity}
    return TrustedHostPorts(client, bindings, rehearsal.catalog, rehearsal.planner, authority, **failback)
