"""IaC modules: versioned, declarative units of destination configuration (T44).

A module checks its params and renders the desired *shape* of the resources it owns. A shape
never holds a secret value: credentials are injected by the applier at apply time and a plan
only names them. Rendering is a pure function, so the same params always give the same plan.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass


class IacError(ValueError):
    """A fixed code. It never carries input text, paths or values."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Module:
    id: str
    version: str
    description: str
    params: dict  # parameter schema shown in the catalog (JSON Schema subset)
    check: Callable[[dict], dict]
    render: Callable[[dict], list[dict]]
    exposure: Callable[[dict], dict]  # what unauthenticated callers may do, for widening checks


# --- target-object-exposure v1 ---------------------------------------------------------------
# The SeaweedFS identity file (s3.json) of the target attachments bucket. Same identities and
# actions as T9 infra/envs/target-workloads/object_exposure.py; tests pin the equality.
BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{3,63}")
CLIENT_ACTIONS = ("Read", "Write", "List", "Tagging")
BOOTSTRAP_ACTIONS = ("Admin",) + CLIENT_ACTIONS
ANONYMOUS_ACTIONS = ("Read",)
TARGET_NAMESPACE = "target-dev"


def _exposure_check(params: dict) -> dict:
    if (type(params) is not dict or not set(params) <= {"state", "bucket", "demo_run_id"}
            or {"state", "bucket"} - set(params)):
        raise IacError("IAC_PARAMS_INVALID")
    state, bucket, run = params["state"], params["bucket"], params.get("demo_run_id")
    if (state not in ("closed", "open-demo") or not isinstance(bucket, str)
            or not BUCKET_RE.fullmatch(bucket) or ".." in bucket):
        raise IacError("IAC_PARAMS_INVALID")
    # open-demo is the demo's planted defect and always names its run; closed never does.
    if (state == "open-demo") != (run is not None) or (
            run is not None and (not isinstance(run, str) or not RUN_ID_RE.fullmatch(run))):
        raise IacError("IAC_PARAMS_INVALID")
    return dict(params)


def _exposure_identities(params: dict) -> list[dict]:
    bucket = params["bucket"]
    identities = [
        {"name": "bootstrap", "actions": [f"{a}:{bucket}" for a in BOOTSTRAP_ACTIONS]},
        {"name": "client", "actions": [f"{a}:{bucket}" for a in CLIENT_ACTIONS]},
    ]
    if params["state"] == "open-demo":
        identities.append({"name": "anonymous", "actions": [f"{a}:{bucket}" for a in ANONYMOUS_ACTIONS]})
    return identities


def _exposure_render(params: dict) -> list[dict]:
    return [{
        "kind": "Secret", "namespace": TARGET_NAMESPACE, "name": "target-seaweedfs-auth", "key": "s3.json",
        "shape": {"identities": _exposure_identities(params)},
        "secret_inputs": ["bootstrap", "client"],  # credentials: injected by the applier, never in Git
        "restart_on_change": ["Deployment/target-dev/target-seaweedfs"],
    }]


def _exposure_exposure(params: dict) -> dict:
    anonymous = [i for i in _exposure_identities(params) if i["name"] == "anonymous"]
    return {"anonymous": sorted(anonymous[0]["actions"]) if anonymous else [],
            "checks": [{"control": "IAM-01.1", "anonymous_get": "ALLOWED" if anonymous else "DENIED"}]}


TARGET_OBJECT_EXPOSURE = Module(
    id="target-object-exposure",
    version="1",
    description="Target attachments bucket identities (SeaweedFS s3.json). closed: app and bootstrap only; "
                "open-demo: also anonymous Read, the demo's planted defect.",
    params={"type": "object", "additionalProperties": False, "required": ["state", "bucket"],
            "properties": {"state": {"enum": ["closed", "open-demo"]},
                           "bucket": {"type": "string", "pattern": BUCKET_RE.pattern},
                           "demo_run_id": {"type": "string", "pattern": RUN_ID_RE.pattern,
                                           "description": "required for open-demo only"}}},
    check=_exposure_check,
    render=_exposure_render,
    exposure=_exposure_exposure,
)
