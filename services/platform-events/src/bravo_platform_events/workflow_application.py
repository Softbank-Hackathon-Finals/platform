"""REHEARSE port for the workflow host: the T31 library applies the approved candidate to the target.

Factory for ports.json `"application": "bravo_platform_events.workflow_application:create"`. It reads
/workflow/application.json (host ConfigMap, names only):

    {"receipts_dir": "/workflow-state/t31-receipts", "kubeconfig": "/t31/kubeconfig",
     "target": {"namespace": "...", "deployment": "...", "container": "app",
                "repository": "localhost/bravo-demo-chat-candidate"},
     "exposure": {"environment_id": "target-dev", "namespace": "target-dev", "bucket": "demo-chat-attachments",
                  "endpoint": "http://seaweedfs.target-dev.svc.cluster.local:8333", "probe_key": "..."}}

`exposure` (optional, ADR-0044): the T44 environment and attachment bucket this host closes when an approval
binds a closing plan. Without it such an approval holds (application_exposure_unavailable).

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


def apply_input(c, plan_id, claim, target, exposure=None):
    """The candidate recorded at PROPOSE under the approved diff_hash, for the registered target.

    ADR-0044: when the approval binds a closing T44 plan, the plan is the one PROPOSE stored with this
    candidate (`infra_plans`) under that digest, closed on the configured exposure target."""
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
    planned, closing = claim["binding"].get("iac_plan_digest"), None
    if planned is not None:
        from bravo_apply.policy import PolicyTarget

        found = [p for p in record.get("infra_plans") or [] if type(p) is dict and p.get("plan_digest") == planned]
        if exposure is None or not found:
            raise EventError("application_exposure_unavailable")
        closing = {"environment_id": exposure["environment_id"], "iac_plan": found[0],
                   "target": PolicyTarget(**{k: v for k, v in exposure.items() if k != "environment_id"})}
    return ApplyInput({"edits": record["edits"]}, diff, claim["binding"]["target_environment_id"],
                      target["namespace"], target["deployment"], target["container"], target["repository"],
                      exposure=closing)


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


def create(store, config_file=CONFIG, *, kube=None, delegations=None):
    from bravo_apply.deploy import KubectlKube

    from . import gate_services
    from .application_host import ApplicationHost, GateRecheck
    from .change import ChangeApprovalService
    from .change_intake import ChangeIntake
    from .operator_access import OperatorAccess
    from .workflow_stages import application_stages

    config = json.loads(Path(config_file).read_text())
    target = config.get("target") if isinstance(config, dict) else None
    exposure = config.get("exposure") if isinstance(config, dict) else None
    if (type(config) is not dict or set(config) - {"exposure"} != {"receipts_dir", "kubeconfig", "target"}
            or type(target) is not dict or set(target) != {"namespace", "deployment", "container", "repository"}
            or not all(isinstance(v, str) and v for v in target.values())
            or (exposure is not None and (type(exposure) is not dict
                or set(exposure) != {"environment_id", "namespace", "endpoint", "probe_key", "bucket"}
                or not all(isinstance(v, str) and v for v in exposure.values())))):
        raise ValueError("workflow_application_configuration_invalid")
    access = OperatorAccess(store)
    changes = ChangeApprovalService(store, authority=access.change_authority(ChangeIntake.current),
                                    check_access=access.check_read)
    kube = kube or KubectlKube(config["kubeconfig"])
    deployed = {}

    def load_input(c, principal, plan, claim):
        return apply_input(c, plan["plan_id"], claim, target, exposure)

    def check_current(c, principal, plan, outcome):
        access.require(c, principal, plan, "apply_change")
        return deployed.get(outcome.application_id) == outcome.deployed_digest

    host = ApplicationHost(changes, kube=kube, receipts_dir=config["receipts_dir"], load_input=load_input)
    records = gate_services.gate_records(store, access)  # one instance for recheck, factory and load_gate
    recheck = GateRecheck(records, prepare=post_apply_prepare(records),
                          service_factory=gate_services.service_factory(records, access))
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

    def observed(stage):
        def prepare(plan_id, tenant_id, version):
            reader = stage(plan_id, tenant_id, version)  # T31 apply / T19 evaluate, outside the plan lock
            observe(plan_id)
            return reader
        return prepare

    return {"REHEARSE": observed(stages["REHEARSE"]), "GATE": observed(stages["GATE"])}
