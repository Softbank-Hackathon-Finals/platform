"""REHEARSE port for the workflow host: the T31 library applies the approved candidate to the target.

Factory for ports.json `"application": "bravo_platform_events.workflow_application:create"`. It reads
/workflow/application.json (host ConfigMap, names only):

    {"receipts_dir": "/workflow-state/t31-receipts", "kubeconfig": "/t31/kubeconfig",
     "target": {"namespace": "...", "deployment": "...", "container": "app",
                "repository": "localhost/bravo-demo-chat-candidate"}}

- Authority: the same S2 rules as the operator API, OperatorAccess.change_authority(ChangeIntake.current);
  the host applies as `workflow-host` (read, apply_change) and the approver is re-checked.
- Input: the edits and diff bytes recorded at PROPOSE under the approved diff_hash; never client JSON.
- Evidence: after T31 reports APPLIED, the deployed image is read once outside the plan lock and must
  equal the receipt's digest; under the lock only stored records are read.
- GATE (T19 recheck after the application): post_apply_prepare binds a post-apply context from the
  operator-recorded target T14 run that started after the application finished (gate_record_host /
  gate_relay); without one, GATE holds. Operator signatures (context, runs, T13 readiness) come through
  the T19 issuer path (#573). gate_services supplies GateRecords, service_factory and load_gate.
"""

import base64
import json
from pathlib import Path

from .store import EventError

CONFIG = "/workflow/application.json"


def apply_input(c, plan_id, claim, target, pull_secret=None):
    """The candidate recorded at PROPOSE under the approved diff_hash, for the registered target."""
    from .application_host import ApplyInput
    from .workflow_propose import apply_input_key

    row = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key=?",
                    (plan_id, apply_input_key(claim["binding"]["diff_hash"]))).fetchone()
    if row is None:
        raise EventError("application_input_unavailable")
    record = json.loads(row["receipt_json"])
    diff = base64.b64decode(record["diff_b64"])
    if record["diff_hash"] != claim["binding"]["diff_hash"]:
        raise EventError("application_input_scope_mismatch")
    return ApplyInput({"edits": record["edits"]}, diff, claim["binding"]["target_environment_id"],
                      target["namespace"], target["deployment"], target["container"], target["repository"],
                      pull_secret)


def post_apply_prepare(records):
    """GateRecheck.prepare: bind the post-apply context once from stored records only.

    Target: the newest recorded target run that started after T31 finished. Source, catalog and
    environment refs: unchanged from the current context. Nothing is collected or signed here.
    """
    from bravo_gate.models import GateContext

    from .executor import _timestamp
    from .gate_records import _current

    def prepare(principal, plan_id, outcome):
        finished = _timestamp(outcome.finished_at)
        with records.store._transaction() as c:
            current = _current(c, plan_id)
            plan = c.execute("SELECT spec_json FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            runs = [json.loads(r["receipt_json"]) for r in c.execute(
                "SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                (plan_id, "gate-run-%")).fetchall()]
        if current is None or plan is None:
            raise EventError("gate_context_required")
        if current.get("application_id") == outcome.application_id:
            return current  # already bound for this application
        spec = json.loads(plan["spec_json"])
        after = [r for r in runs if r.get("side") == "target" and "ref" in r
                 and _timestamp(r["document"]["started_at"]) >= finished]
        if not after:
            raise EventError("post_application_target_run_required")  # operator records it, then retry
        run = max(after, key=lambda r: _timestamp(r["document"]["started_at"]))
        ctx = current["context"]
        context = GateContext(**(ctx | dict(
            gate_id="post-apply-" + outcome.application_id, profile_id=spec["recheck_profile_id"],
            profile_version=spec["recheck_profile_version"], target_ref=run["ref"], target_digest=run["digest"])))
        return records.bind(principal, plan_id, "post-apply-" + outcome.application_id, context,
                            expected_revision=current["revision"], gate_phase="post_apply",
                            application_id=outcome.application_id)

    return prepare


def platform_signer(store, records, access, delegations, loaded):
    """GateRecheck.sign (ADR-0038): the platform KMS keys sign the bound post-apply context under the plan's
    live `evidence` delegation for the GATE version; without one nothing is signed and GATE holds."""
    from datetime import UTC, datetime

    from . import gate_relay, gate_signer
    from .cutover import _get, _put

    relay, keys = loaded

    def call(request):
        return gate_relay.handle(records, request, relay, access=access)

    def put_record(plan_id, key, value):
        with store._transaction(write=True, plan_id=plan_id) as c:
            if _get(c, plan_id, key) is None:  # a retry keeps the first record
                _put(c, plan_id, key, {"op": "platform-sign"}, value)

    def sign(principal, plan_id, outcome):
        with store._transaction(plan_id=plan_id) as c:
            version = c.execute("SELECT version FROM plans WHERE plan_id=?", (plan_id,)).fetchone()["version"]
        gate_signer.sign(call, keys, plan_id, datetime.now(UTC), put_record,
                         delegation=gate_signer.delegated(delegations, plan_id, version))

    return sign


def create(store, config_file=CONFIG, *, kube=None, delegations=None, signer=None):
    """`signer`: (relay config, KMS keys) for the platform signer; by default from /workflow/gate-signer.json
    when that file is mounted. It needs `delegations` (the evidence check)."""
    from bravo_apply.deploy import KubectlKube

    from . import gate_services
    from .application_host import ApplicationHost, GateRecheck
    from .change import ChangeApprovalService
    from .change_intake import ChangeIntake
    from .operator_access import OperatorAccess
    from .workflow_stages import application_stages

    config = json.loads(Path(config_file).read_text())
    target = config.get("target") if isinstance(config, dict) else None
    pull = config.get("pull_secret") if isinstance(config, dict) else None
    if (type(config) is not dict or not {"receipts_dir", "kubeconfig", "target"} <= set(config)
            or set(config) - {"receipts_dir", "kubeconfig", "target", "pull_secret"}
            or (pull is not None and (type(pull) is not dict or set(pull) != {"name", "source"}))
            or type(target) is not dict or set(target) != {"namespace", "deployment", "container", "repository"}
            or not all(isinstance(v, str) and v for v in target.values())):
        raise ValueError("workflow_application_configuration_invalid")
    access = OperatorAccess(store)
    changes = ChangeApprovalService(store, authority=access.change_authority(ChangeIntake.current),
                                    check_access=access.check_read)
    kube = kube or KubectlKube(config["kubeconfig"])
    deployed = {}

    def load_input(c, principal, plan, claim):
        return apply_input(c, plan["plan_id"], claim, target, pull and pull["name"])

    def check_current(c, principal, plan, outcome):
        access.require(c, principal, plan, "apply_change")
        return deployed.get(outcome.application_id) == outcome.deployed_digest

    host = ApplicationHost(changes, kube=kube, receipts_dir=config["receipts_dir"], load_input=load_input)
    records = gate_services.gate_records(store, access)  # one instance for recheck, factory and load_gate
    from . import gate_signer

    if signer is None and Path(gate_signer.CONFIG).is_file():
        signer = gate_signer.load()
    if signer is not None and delegations is None:
        raise ValueError("workflow_platform_signer_needs_delegations")  # never sign without the check
    recheck = GateRecheck(records, prepare=post_apply_prepare(records),
                          service_factory=gate_services.service_factory(records, access),
                          sign=platform_signer(store, records, access, delegations, signer) if signer else None)
    stages = application_stages(host, recheck, receipts_dir=config["receipts_dir"], check_current=check_current,
                                load_gate=gate_services.load_gate(records, access), host_subject="workflow-host",
                                delegations=delegations)

    def observe(plan_id):
        """Read the deployed image once, outside the plan lock (also after a host restart)."""
        from bravo_apply import get_result

        for path in Path(config["receipts_dir"]).glob("*.json"):
            outcome = get_result(path.stem, Path(config["receipts_dir"]))
            if outcome is not None and outcome.plan_id == plan_id and outcome.deployed_digest:
                image = kube.current_image(target["namespace"], target["deployment"], target["container"])
                deployed[outcome.application_id] = image.rsplit("@", 1)[-1] if "@" in image else None

    def refresh_pull():
        """T49 (11/8 user decision): the platform's current, expiring GHCR read credential into the target's
        one pull Secret before an apply, so the candidate is pulled by digest. Written only when it changed;
        the value goes through stdin (KubectlKube.replace_secret) and is never logged."""
        value = base64.b64encode(Path(pull["source"]).read_bytes()).decode()
        secret = kube.get_secret(target["namespace"], pull["name"])
        if (secret.get("data") or {}).get(".dockerconfigjson") != value:
            secret.pop("stringData", None)
            kube.replace_secret(target["namespace"], secret | {"data": {".dockerconfigjson": value}})

    def observed(stage, *, applies=False):
        def prepare(plan_id, tenant_id, version):
            if applies and pull is not None:
                refresh_pull()
            reader = stage(plan_id, tenant_id, version)  # T31 apply / T19 evaluate, outside the plan lock
            observe(plan_id)
            return reader
        return prepare

    return {"REHEARSE": observed(stages["REHEARSE"], applies=True), "GATE": observed(stages["GATE"])}
