"""Node files: settings a cluster reads from its nodes, outside the Kubernetes API (T44).

The API server reads some settings only from a file on the node, at start. A GitOps controller
inside the cluster cannot own them, so a node-file module renders the file's exact bytes and an
applier on the node installs them: the bootstrap of a new node, or infra/iac/nodes/apply.py on a
running one. ``files`` writes them under infra/iac/nodes/<environment>/ at their node path, with a
manifest.json the applier checks before it installs anything.

cluster-audit v1: the API server audit policy. The bytes are the reviewed policy of T30/T34
(services/control-runner/templates/rke2-audit-policy.yaml); a test pins the packaged copy to it.
"""

import hashlib
import json
from pathlib import Path

from .modules import IacError, Module, _exact

KIND = "NodeFile"
# Where each verified runtime's API server reads its audit policy. A managed control plane (EKS,
# GKE) has no node file: its audit log is the provider's control-plane logging, so it is refused.
AUDIT_POLICY_PATH = {"rke2": "/etc/rancher/rke2/audit-policy.yaml"}
AUDIT_POLICIES = {"metadata-v1": "rke2-audit-policy.yaml"}
API_SERVER = "StaticPod/kube-system/kube-apiserver"


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def audit_policy(name: str) -> str:
    # Next to this file, not resources.files("bravo_iac"): T31 loads this package under another name.
    return (Path(__file__).parent / "data" / AUDIT_POLICIES[name]).read_text(encoding="utf-8")


def _audit_check(params: dict) -> dict:
    _exact(params, ("runtime", "policy"))
    runtime, policy = params["runtime"], params["policy"]
    if not isinstance(runtime, str) or not isinstance(policy, str):
        raise IacError("IAC_PARAMS_INVALID")
    if runtime not in AUDIT_POLICY_PATH:
        raise IacError("IAC_RUNTIME_UNSUPPORTED")
    if policy not in AUDIT_POLICIES:
        raise IacError("IAC_PARAMS_INVALID")
    return {"runtime": runtime, "policy": policy}


def _audit_render(params: dict) -> list[dict]:
    content = audit_policy(params["policy"])
    return [{"kind": KIND, "namespace": "-", "name": "audit-policy",
             "shape": {"path": AUDIT_POLICY_PATH[params["runtime"]], "mode": "0600", "sha256": _sha(content)},
             "content": content,
             # The API server reads the policy only at start: the applier restarts it after installing.
             "restart_on_change": [API_SERVER]}]


def _audit_exposure(params: dict) -> dict:
    return {"anonymous": [], "reach": [], "checks": [f'audit-policy:{params["policy"]}']}


CLUSTER_AUDIT = Module(
    id="cluster-audit",
    version="1",
    description="The API server audit policy as a node file (Metadata for Secrets, RBAC and other requests; "
                "never request or response bodies). Applied by the node bootstrap or infra/iac/nodes/apply.py.",
    params={"type": "object", "additionalProperties": False, "required": ["runtime", "policy"],
            "properties": {"runtime": {"enum": sorted(AUDIT_POLICY_PATH)},
                           "policy": {"enum": sorted(AUDIT_POLICIES)}}},
    check=_audit_check,
    render=_audit_render,
    exposure=_audit_exposure,
)


def _node_files(state) -> tuple[dict, list[dict]]:
    from .catalog import check_state, module  # the catalog imports this module

    state = check_state(state)
    contents, rows = {}, []
    for entry in state["modules"]:
        for resource in module(entry["id"], entry["version"]).render(entry["params"]):
            if resource["kind"] != KIND:
                continue
            shape = resource["shape"]
            contents[shape["path"]] = resource["content"]
            rows.append({"path": shape["path"], "mode": shape["mode"], "sha256": shape["sha256"],
                         "restart": resource.get("restart_on_change", []), "module": entry["id"]})
    return state, sorted(rows, key=lambda r: r["path"])


def listing(state) -> dict:
    """What an applier must install on this environment's nodes (GET .../node-files)."""
    from .plan import digest

    state, rows = _node_files(state)
    environment = state["environment"]
    return {"kind": "iac-node-files.v1", "environment": environment, "state_digest": digest(state),
            "files": [{**r, "href": f"/v1/iac/environments/{environment}/node-files{r['path']}"} for r in rows]}


def read(state, path: str) -> dict:
    """One node file with its exact content (GET .../node-files/{path})."""
    from .catalog import check_state, module

    for row in listing(state)["files"]:
        if row["path"] == path:
            for entry in check_state(state)["modules"]:
                for resource in module(entry["id"], entry["version"]).render(entry["params"]):
                    if resource["kind"] == KIND and resource["shape"]["path"] == path:
                        return {**row, "content": resource["content"]}
    raise IacError("IAC_NODE_FILE_UNKNOWN")


def files(state) -> dict[str, str]:
    """relative path under infra/iac/nodes/<environment>/ -> exact content, plus manifest.json."""
    _, rows = _node_files(state)
    out = {path.lstrip("/"): read(state, path)["content"] for path in (r["path"] for r in rows)}
    out["manifest.json"] = json.dumps(listing(state), indent=2, ensure_ascii=False) + "\n"
    return out
