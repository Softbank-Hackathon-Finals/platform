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
- GATE (T19 recheck after the application) is not composed here yet: GATE holds as unwired.
"""

import base64
import json
from pathlib import Path

from .store import EventError

CONFIG = "/workflow/application.json"


def _unwired(*_):
    raise EventError("workflow_reader_unwired")


def apply_input(c, plan_id, claim, target):
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
                      target["namespace"], target["deployment"], target["container"], target["repository"])


def create(store, config_file=CONFIG, *, kube=None):
    from bravo_apply.deploy import KubectlKube

    from .application_host import ApplicationHost
    from .change import ChangeApprovalService
    from .change_intake import ChangeIntake
    from .operator_access import OperatorAccess
    from .workflow_stages import application_stages

    config = json.loads(Path(config_file).read_text())
    target = config.get("target") if isinstance(config, dict) else None
    if (type(config) is not dict or set(config) != {"receipts_dir", "kubeconfig", "target"}
            or type(target) is not dict or set(target) != {"namespace", "deployment", "container", "repository"}
            or not all(isinstance(v, str) and v for v in target.values())):
        raise ValueError("workflow_application_configuration_invalid")
    access = OperatorAccess(store)
    changes = ChangeApprovalService(store, authority=access.change_authority(ChangeIntake.current),
                                    check_access=access.check_read)
    kube = kube or KubectlKube(config["kubeconfig"])
    deployed = {}

    def load_input(c, principal, plan, claim):
        return apply_input(c, plan["plan_id"], claim, target)

    def check_current(c, principal, plan, outcome):
        access.require(c, principal, plan, "apply_change")
        return deployed.get(outcome.application_id) == outcome.deployed_digest

    host = ApplicationHost(changes, kube=kube, receipts_dir=config["receipts_dir"], load_input=load_input)
    stages = application_stages(host, None, receipts_dir=config["receipts_dir"], check_current=check_current,
                                load_gate=_unwired, host_subject="workflow-host")
    rehearse = stages["REHEARSE"]

    def rehearse_then_observe(plan_id, tenant_id, version):
        reader = rehearse(plan_id, tenant_id, version)  # T31 apply happens here, outside the plan lock
        from bravo_apply import get_result

        for path in Path(config["receipts_dir"]).glob("*.json"):
            outcome = get_result(path.stem, Path(config["receipts_dir"]))
            if outcome is not None and outcome.plan_id == plan_id and outcome.deployed_digest:
                image = kube.current_image(target["namespace"], target["deployment"], target["container"])
                deployed[outcome.application_id] = image.rsplit("@", 1)[-1] if "@" in image else None
        return reader

    return {"REHEARSE": rehearse_then_observe}
