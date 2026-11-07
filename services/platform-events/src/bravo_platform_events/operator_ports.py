"""Production ports for the operator approval host (`approval_server --ports ...operator_ports:create`).

The approval API (change, SHIFT and cutover approvals, failback) runs only through
`trusted_rehearsal.build_trusted_host`, which needs TrustedHostPorts. One protected JSON names mounted
files only (`/rehearsal/operator.json`):

    {"executor": {"url": "https://cutover-executor.bravo-platform.svc:8080", "ca_file": "/executor/ca.crt",
                  "token_file": "/executor/read-token", "command_token_file": "/executor/command-token",
                  "registered_pair": {...}},
     "gate": "module:factory",              # T19: factory(store) -> (GateRecords, load_gate)
     "failback": true,                     # optional T20: current_failback + failback_integrity
     "edge": {"journal": "/records/edge/journal.db", "token_file": "/edge/token",
              "lb_binding": {...}},          # optional T17: the one LB journal; the SHIFT CLI runs in
                                            # this Pod (kubectl exec ... bravo_edge_control.run) on it
     "iac": {"app_id": 1, "key_file": "/iac/app.pem",
             "repository": "Softbank-Hackathon-Finals/platform"},  # optional T44: desired-state PRs
                                            # after a T10 infra approval (iac_plans); contents+PR only
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
    """The approval that moved this plan into its current SHIFT (recorded one version earlier).

    Only that version: after a rollback and a new SHIFT, an older attempt's approval never counts.
    """
    row = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key=?",
                    (plan["plan_id"], f"shift-approval-{plan['version'] - 1}")).fetchone()
    return json.loads(row["receipt_json"]) if row is not None else None


def _ts(value):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def edge_at_full(c, plan):
    """T10-cutover-submit: this SHIFT really reached 100% and passed the SLI judgement at 100%.

    From the T17 ShiftHost records only: the newest CONFIRMED stage since the plan entered SHIFT targets
    100, and the newest judgement of that stage is PASS. An older SHIFT attempt never counts.
    """
    rows = c.execute("SELECT command_key,receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                     (plan["plan_id"], "edge-host-%")).fetchall()
    entered = _ts(plan["updated_at"])
    stages = [json.loads(r["receipt_json"]) for r in rows if r["command_key"].startswith("edge-host-receipt-")]
    stages = [s for s in stages if s.get("status") == "CONFIRMED" and s.get("confirmed_at")
              and _ts(s["confirmed_at"]) >= entered]
    if not stages:
        return False
    last = max(stages, key=lambda s: _ts(s["confirmed_at"]))
    prefix = f"edge-host-judgement-{last['revision']:013}-"
    judged = sorted(r["command_key"] for r in rows if r["command_key"].startswith(prefix))
    if last["weight_target"] != 100 or not judged:
        return False
    newest = next(r for r in rows if r["command_key"] == judged[-1])
    return json.loads(newest["receipt_json"])["judgement"]["verdict"] == "PASS"


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
        if not edge_at_full(c, plan):
            raise EventError("shift_not_at_full")
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


def current_failback(bindings, *, write_pause_budget_ms=30000, window_hours=2):
    """T20 upstream: reverse the committed cutover (target -> source), from stored records only.

    Binding: the cutover run and its matched integrity checkpoint (failback.cutover_record). Epoch: one
    above every epoch the plan used. Expiry: the time the plan entered its current version (WATCH) plus
    window_hours, so approval and submission at the same version compute the same context.
    """
    from datetime import datetime, timedelta

    from .failback import CurrentFailback, cutover_record, used_epochs

    if not isinstance(bindings, dict) or len(bindings) != 2:
        raise ValueError("failback_policy_configuration_invalid")

    def current(c, principal, plan, *, now=None, approved_by=None, approval_id=None, **_selection):
        spec = json.loads(plan["spec_json"])
        run = cutover_record(c, plan["plan_id"])
        entered = datetime.fromisoformat(plan["updated_at"].replace("Z", "+00:00"))
        expires = (entered + timedelta(hours=window_hours)).isoformat().replace("+00:00", "Z")
        source, target = spec["target_environment_id"], spec["source_environment_id"]  # reversed
        if {source, target} != set(bindings):
            raise EventError("authority_unavailable")
        binding = dict(plan_id=plan["plan_id"], plan_digest=spec["plan_digest"], source_environment_id=source,
                       target_environment_id=target, cutover_execution_id=run["execution_id"],
                       cutover_epoch=run["epoch"], integrity_receipt_id=run["integrity_receipt_id"],
                       expires_at=expires)
        command = dict(plan_id=plan["plan_id"], epoch=max(used_epochs(c, plan["plan_id"]), default=0) + 1,
                       source=dict(environment_id=source, connection_ref=bindings[source]),
                       target=dict(environment_id=target, connection_ref=bindings[target]),
                       write_pause_budget_ms=write_pause_budget_ms, deadline=expires)
        revision = "operations-" + hashlib.sha256(json.dumps([binding, command], sort_keys=True).encode()).hexdigest()[:32]
        return CurrentFailback(binding, command, revision)

    return current


def failback_integrity(c, plan, *, command_id, epoch, execution_id):
    """The Executor's own T13 comparison for the failback run (direction=failback receipt)."""
    from .cutover import latest_observation
    from .failback import FailbackIntegrity, failback_key

    snapshot = latest_observation(c, plan["plan_id"], command_id, failback_key)["snapshot"] or {}
    steps = {s["step"]: s["status"] for s in snapshot.get("steps", [])}
    matched = (snapshot.get("remote_status") == "DONE" and snapshot.get("committed") is True
               and snapshot.get("execution_id") == execution_id and snapshot.get("epoch") == epoch
               and steps.get("compare") == "succeeded" and steps.get("commit") == "succeeded")
    return FailbackIntegrity("integrity-" + execution_id, command_id, epoch, execution_id, matched)


def _edge(config):
    from bravo_edge_control.control import Controller, FailbackEdge, journal_failback_command
    from bravo_edge_control.provider import Binding, Cloudflare

    if type(config) is not dict or set(config) != {"journal", "token_file", "lb_binding"}:
        raise ValueError("operator_edge_configuration_invalid")
    journal, token = Path(config["journal"]), Path(config["token_file"])
    if not journal.is_absolute() or not journal.parent.is_dir() or not token.is_file():
        raise ValueError("operator_edge_mount_required")
    provider = Cloudflare(Binding(**config["lb_binding"]), token=lambda: token.read_text().strip())
    # FailbackService calls Edge only after the matched failback receipt; the journal still refuses
    # a stale revision, a pending operation or a weight other than the confirmed one.
    controller = Controller(provider, journal, authorize=lambda command: command.reason == "FAILBACK")
    return FailbackEdge(controller, journal_failback_command(controller))


def create(store, config_file=CONFIG):
    import importlib
    import ssl

    from .credential_host import create_ports as rehearsal_ports
    from .executor import ExecutorClient
    from .trusted_rehearsal import TrustedHostPorts

    config = json.loads(Path(config_file).read_text())
    executor = config.get("executor") if isinstance(config, dict) else None
    if (type(config) is not dict or not {"executor", "gate"} <= set(config) <= {"executor", "gate", "write_pause_budget_ms", "failback", "edge", "iac"}
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
    if config.get("failback") is True:
        failback = {"failback_authority": current_failback(bindings, write_pause_budget_ms=config.get("write_pause_budget_ms", 30000)),
                    "read_integrity": failback_integrity}
        if "edge" in config:  # absent: FAILBACK stops at edge_unavailable after the matched receipt
            failback["edge"] = _edge(config["edge"])
    iac = {"iac": _iac(config["iac"])} if "iac" in config else {}
    return TrustedHostPorts(client, bindings, rehearsal.catalog, rehearsal.planner, authority, **failback, **iac)


def _iac(value):
    """T44: the platform repository through its own GitHub App installation (contents + pull requests)."""
    from .github_app import GitHubApp
    from .iac_plans import WRITER_PERMISSIONS, GitHubIacRepository

    if (type(value) is not dict or set(value) != {"app_id", "key_file", "repository"}
            or type(value["app_id"]) is not int or value["repository"] != "Softbank-Hackathon-Finals/platform"):
        raise ValueError("operator_ports_configuration_invalid")
    key = Path(value["key_file"]).read_bytes()
    return GitHubIacRepository(GitHubApp(value["app_id"], key, value["repository"], WRITER_PERMISSIONS),
                               value["repository"])
