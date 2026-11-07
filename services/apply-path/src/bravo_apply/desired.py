"""Apply a merged T44 desired state of the target bucket exposure, through the bucket-policy branch.

T44 (ADR-0032) keeps the target's attachments-bucket exposure as the module
``target-object-exposure`` in ``infra/iac/environments/<environment>.json``. Its chart cannot carry
the bucket identities (they hold secret values), so once the desired state changes the Secret is
applied here, by the same code that applies an approved bucket policy (``policy.py``), and the gate is
asked to re-check IAM-01.1. There is one writer of that Secret (#646).

What this accepts, and nothing else:

1. ``revision`` is a full commit SHA reachable from ``reviewed_ref`` (the protected branch the
   operator promotes from; develop takes no approval). The desired state is read from that commit
   with ``git show``, never from the working tree.
2. The module there says ``closed`` for the target's bucket, and the reviewed branch's tip still says
   the same (an older revision cannot undo a newer one). ``open-demo`` is refused: T31 never opens.
3. The T44 plan that staged the change (``bravo-iac plan``) is given: its ``plan_digest`` checks out,
   it was accepted, its ``proposed_digest`` is the state at ``revision`` and it removes anonymous
   access in this module. Digests are computed by the T44 package itself (``check_state`` and
   ``digest``), loaded from the repository like the T9 renderer, so the two never drift.
4. One record per ``application_id`` (``iac-<environment>-<revision[:16]>``), created before the
   cluster is touched. A second call returns that record and changes nothing.
5. A target already without the ``anonymous`` identity is left as it is (``ALREADY_CLOSED``).

``apply_planned_exposure`` (ADR-0044) is the run-time path of the demo: Git keeps ``open-demo`` and an
approved agent ``infra.plan`` closing this bucket is the input instead of a reviewed revision.

Secret values never leave ``policy.py``: records carry sha256 digests of ``s3.json`` only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from types import ModuleType

from .deploy import DeployMismatch, Kube
from .path import POLICY_GATE_RECHECK_TESTS
from .policy import (
    PolicyRefused,
    PolicyTarget,
    _secret_bytes,
    apply_bucket_policy,
    load_t9,
    render_closed,
)

MODULE, MODULE_VERSION = "target-object-exposure", "1"
ENVIRONMENTS = "infra/iac/environments"
REVISION = re.compile(r"^[0-9a-f]{40}$")
ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
# T44 owns the desired-state schema and its digests; standard library only. parents[4] is the repository root.
T44_PACKAGE = Path(__file__).resolve().parents[4] / "services/iac/src/bravo_iac"


class DesiredRefused(ValueError):
    """The request or the desired state is not one T31 applies; nothing was recorded or changed."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@lru_cache(maxsize=2)
def load_t44(package: Path = T44_PACKAGE) -> ModuleType:
    name = "bravo_apply._t44_iac"
    try:
        spec = importlib.util.spec_from_file_location(name, Path(package) / "__init__.py",
                                                      submodule_search_locations=[str(package)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # the package's relative imports resolve under this name
        spec.loader.exec_module(module)
    except (OSError, ImportError, AttributeError):
        sys.modules.pop(name, None)
        raise DesiredRefused("T44_PACKAGE_UNAVAILABLE") from None
    return module


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, timeout=60, check=False)


def _module(repo_root: Path, ref: str, environment_id: str) -> tuple[dict, str, dict]:
    """The exposure module's params at ``ref``, the sha256 of the environment file there, and the file."""
    shown = _git(repo_root, "show", f"{ref}:{ENVIRONMENTS}/{environment_id}.json")
    if shown.returncode != 0:
        raise DesiredRefused("DESIRED_STATE_UNAVAILABLE")
    try:
        doc = json.loads(shown.stdout)
    except ValueError:
        raise DesiredRefused("DESIRED_STATE_UNRECOGNIZED") from None
    if not isinstance(doc, dict) or doc.get("environment") != environment_id:
        raise DesiredRefused("DESIRED_STATE_ENVIRONMENT_MISMATCH")
    found = [m for m in doc.get("modules") or [] if isinstance(m, dict) and m.get("id") == MODULE]
    if len(found) != 1 or found[0].get("version") != MODULE_VERSION or not isinstance(found[0].get("params"), dict):
        raise DesiredRefused("DESIRED_STATE_UNRECOGNIZED")
    return found[0]["params"], hashlib.sha256(shown.stdout).hexdigest(), doc


def check_closing_plan(t44: ModuleType, plan: dict, environment_id: str) -> str:
    """A self-consistent, accepted T44 plan for this environment that removes anonymous access to the
    attachment bucket. Returns its ``plan_digest``. Which desired state it proposes is the caller's check
    (``check_iac_plan`` for a Git revision; T10 binds the digest into the change approval, ADR-0044)."""
    if type(plan) is not dict or plan.get("kind") != "iac-plan.v1" or plan.get("environment") != environment_id:
        raise DesiredRefused("IAC_PLAN_INVALID")
    body = {k: v for k, v in plan.items() if k != "plan_digest"}
    if t44.digest(body) != plan.get("plan_digest"):
        raise DesiredRefused("IAC_PLAN_DIGEST_MISMATCH")
    if plan.get("accepted") is not True or plan.get("refusals"):
        raise DesiredRefused("IAC_PLAN_NOT_ACCEPTED")
    closing = [e for e in plan.get("effects") or [] if isinstance(e, dict) and e.get("module") == MODULE
               and isinstance(e.get("anonymous_access"), dict)]
    if len(closing) != 1 or closing[0]["anonymous_access"].get("after") != [] \
            or not closing[0]["anonymous_access"].get("before"):
        raise DesiredRefused("IAC_PLAN_DOES_NOT_CLOSE")
    return plan["plan_digest"]


def check_iac_plan(t44: ModuleType, plan: dict, state: dict, environment_id: str) -> str:
    """The T44 plan that staged ``state``: self-consistent, accepted, for this state, and closing."""
    if type(plan) is not dict or plan.get("kind") != "iac-plan.v1" or plan.get("environment") != environment_id:
        raise DesiredRefused("IAC_PLAN_INVALID")
    body = {k: v for k, v in plan.items() if k != "plan_digest"}
    if t44.digest(body) != plan.get("plan_digest"):
        raise DesiredRefused("IAC_PLAN_DIGEST_MISMATCH")
    if plan.get("accepted") is not True or plan.get("refusals"):
        raise DesiredRefused("IAC_PLAN_NOT_ACCEPTED")
    try:
        state_digest = t44.digest(t44.check_state(state))
    except t44.IacError:
        raise DesiredRefused("DESIRED_STATE_UNRECOGNIZED") from None
    if plan.get("proposed_digest") != state_digest:
        raise DesiredRefused("IAC_PLAN_NOT_THIS_REVISION")
    check_closing_plan(t44, plan, environment_id)
    return state_digest


def read_desired(repo_root: Path, environment_id: str, revision: str, reviewed_ref: str, bucket: str,
                 iac_plan: dict) -> dict:
    """Check the request against Git only (no cluster): what ``revision`` asks for, or ``DesiredRefused``."""
    if not ENVIRONMENT.fullmatch(environment_id or ""):
        raise DesiredRefused("ENVIRONMENT_INVALID")
    if not REVISION.fullmatch(revision or ""):
        raise DesiredRefused("REVISION_NOT_FULL_SHA")
    if not REF.fullmatch(reviewed_ref or "") or ".." in reviewed_ref:
        raise DesiredRefused("REVIEWED_REF_INVALID")
    if _git(repo_root, "cat-file", "-e", f"{revision}^{{commit}}").returncode != 0:
        raise DesiredRefused("REVISION_UNKNOWN")
    if _git(repo_root, "merge-base", "--is-ancestor", revision, reviewed_ref).returncode != 0:
        raise DesiredRefused("REVISION_NOT_REVIEWED")
    params, file_sha256, state = _module(repo_root, revision, environment_id)
    if params.get("state") == "open-demo":
        raise DesiredRefused("T31_NEVER_OPENS")
    if params.get("state") != "closed" or set(params) != {"state", "bucket"}:
        raise DesiredRefused("DESIRED_STATE_UNRECOGNIZED")
    if params["bucket"] != bucket:
        raise DesiredRefused("DESIRED_BUCKET_MISMATCH")
    if _module(repo_root, reviewed_ref, environment_id)[0] != params:
        raise DesiredRefused("DESIRED_STATE_SUPERSEDED")
    state_digest = check_iac_plan(load_t44(), iac_plan, state, environment_id)
    return {"environment_id": environment_id, "revision": revision, "reviewed_ref": reviewed_ref,
            "module": MODULE, "module_version": MODULE_VERSION, "state": "closed", "bucket": bucket,
            "environment_file_sha256": file_sha256, "state_digest": state_digest,
            "iac_plan_digest": iac_plan["plan_digest"]}


def application_id_for(environment_id: str, revision: str) -> str:
    return f"iac-{environment_id}-{revision[:16]}"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _path(receipts_dir: Path, application_id: str) -> Path:
    return Path(receipts_dir) / f"{application_id}.json"


def get_desired_result(application_id: str, receipts_dir: Path) -> dict | None:
    """The recorded outcome, or ``None``. Never calls the cluster."""
    try:
        return json.loads(_path(receipts_dir, application_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _write_temp(directory: Path, record: dict) -> str:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{record['application_id']}.", suffix=".tmp", dir=directory)  # 0600
    try:
        os.write(fd, (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return tmp


def _create(directory: Path, record: dict) -> bool:
    """Publish the record only if none exists (complete bytes or nothing), like ``path._create_record``."""
    tmp = _write_temp(directory, record)
    try:
        os.link(tmp, _path(directory, record["application_id"]))
    except FileExistsError:
        return False
    finally:
        os.unlink(tmp)
    return True


def _replace(directory: Path, record: dict) -> None:
    os.replace(_write_temp(directory, record), _path(directory, record["application_id"]))


def apply_desired_exposure(environment_id: str, revision: str, *, reviewed_ref: str, iac_plan: dict, repo_root: Path,
                           kube: Kube, receipts_dir: Path, target: PolicyTarget, plan_id: str | None = None,
                           timeout_seconds: int = 300, policy_hooks: dict | None = None) -> dict:
    """Make the target's ``s3.json`` match a reviewed ``closed`` desired state, then request IAM-01.1.

    Raises ``DesiredRefused`` before anything is recorded when Git does not ask for this change.
    Otherwise returns the record: ``APPLIED``, ``ALREADY_CLOSED``, ``REFUSED`` (cluster untouched)
    or ``SAFE_HOLD`` (the Secret may have changed; a person looks, nothing is rolled back)."""
    desired = read_desired(Path(repo_root), environment_id, revision, reviewed_ref, target.bucket, iac_plan)
    recheck = {"environment_id": environment_id, "revision": revision, "iac_plan_digest": desired["iac_plan_digest"]}
    return _close(application_id_for(environment_id, revision), desired, recheck, applied="DESIRED_STATE_APPLIED",
                  kube=kube, receipts_dir=receipts_dir, target=target, plan_id=plan_id,
                  timeout_seconds=timeout_seconds, policy_hooks=policy_hooks)


def read_planned(repo_root: Path, environment_id: str, base_revision: str, iac_plan: dict,
                 approved_iac_plan_digest: str, bucket: str) -> dict:
    """ADR-0044: check an agent's accepted ``infra.plan`` that closes the bucket (Git only, no cluster).

    The plan must be exactly what T44 computes for "this module, ``closed``, nothing else" from the
    desired state committed at ``base_revision`` (the state the proposal job planned against), and it
    must be the one the person approved (``approved_iac_plan_digest`` from the T10 binding). Git keeps
    ``open-demo`` as the demo's starting state; this closes it at run time."""
    if not ENVIRONMENT.fullmatch(environment_id or ""):
        raise DesiredRefused("ENVIRONMENT_INVALID")
    if not REVISION.fullmatch(base_revision or ""):
        raise DesiredRefused("REVISION_NOT_FULL_SHA")
    if type(iac_plan) is not dict or iac_plan.get("kind") != "iac-plan.v1" or iac_plan.get("origin") not in ("agent", "operator"):
        raise DesiredRefused("IAC_PLAN_INVALID")
    if not isinstance(approved_iac_plan_digest, str) or approved_iac_plan_digest != iac_plan.get("plan_digest"):
        raise DesiredRefused("IAC_PLAN_NOT_APPROVED")
    if _git(repo_root, "cat-file", "-e", f"{base_revision}^{{commit}}").returncode != 0:
        raise DesiredRefused("REVISION_UNKNOWN")
    params, file_sha256, state = _module(repo_root, base_revision, environment_id)
    if params.get("bucket") != bucket:
        raise DesiredRefused("DESIRED_BUCKET_MISMATCH")
    if params.get("state") != "open-demo":
        raise DesiredRefused("PLANNED_BASE_NOT_OPEN")  # nothing for a closing plan to close
    t44 = load_t44()
    if t44.digest({k: v for k, v in iac_plan.items() if k != "plan_digest"}) != iac_plan.get("plan_digest"):
        raise DesiredRefused("IAC_PLAN_DIGEST_MISMATCH")
    proposed = json.loads(json.dumps(state))
    next(m for m in proposed["modules"] if m.get("id") == MODULE)["params"] = {"state": "closed", "bucket": bucket}
    try:
        expected = t44.plan(state, proposed, origin=iac_plan["origin"])
    except t44.IacError:
        raise DesiredRefused("DESIRED_STATE_UNRECOGNIZED") from None
    if iac_plan.get("base_digest") != expected["base_digest"]:
        raise DesiredRefused("IAC_PLAN_NOT_THIS_BASE")
    if iac_plan != expected:
        raise DesiredRefused("IAC_PLAN_NOT_A_CLOSE")  # it changes more, or other than, this bucket's exposure
    if not expected["accepted"]:
        raise DesiredRefused("IAC_PLAN_NOT_ACCEPTED")
    return {"environment_id": environment_id, "base_revision": base_revision, "module": MODULE,
            "module_version": MODULE_VERSION, "state": "closed", "bucket": bucket,
            "environment_file_sha256": file_sha256, "base_digest": expected["base_digest"],
            "state_digest": expected["proposed_digest"], "iac_plan_digest": expected["plan_digest"],
            "origin": expected["origin"]}


def planned_application_id(iac_plan_digest: str) -> str:
    return f"iac-{iac_plan_digest[:16]}"


def apply_planned_exposure(iac_plan: dict, *, approved_iac_plan_digest: str, environment_id: str, base_revision: str,
                           repo_root: Path, kube: Kube, receipts_dir: Path, target: PolicyTarget,
                           plan_id: str | None = None, timeout_seconds: int = 300,
                           policy_hooks: dict | None = None) -> dict:
    """ADR-0044 REHEARSE: close the bucket from the approved agent plan, then request IAM-01.1.

    Same outcomes and records as ``apply_desired_exposure``; ``application_id`` is
    ``iac-<plan_digest[:16]>`` so a repeated call returns the first record."""
    desired = read_planned(Path(repo_root), environment_id, base_revision, iac_plan, approved_iac_plan_digest,
                           target.bucket)
    recheck = {"environment_id": environment_id, "base_revision": base_revision,
               "iac_plan_digest": desired["iac_plan_digest"]}
    return _close(planned_application_id(desired["iac_plan_digest"]), desired, recheck, applied="PLANNED_CLOSE_APPLIED",
                  kube=kube, receipts_dir=receipts_dir, target=target, plan_id=plan_id,
                  timeout_seconds=timeout_seconds, policy_hooks=policy_hooks)


def _close(application_id: str, desired: dict, recheck_fields: dict, *, applied: str, kube: Kube, receipts_dir: Path,
           target: PolicyTarget, plan_id: str | None, timeout_seconds: int, policy_hooks: dict | None) -> dict:
    """One record per ``application_id``, then the bucket-policy branch: the part both entry points share."""
    receipts_dir = Path(receipts_dir)
    existing = get_desired_result(application_id, receipts_dir)
    if existing is not None:
        return existing
    record = {"schema_version": 1, "application_id": application_id, "desired": desired, "target": target.record(),
              "cluster": getattr(kube, "cluster", "unknown"), "plan_id": plan_id, "status": "STARTED", "reason": None,
              "cluster_mutation_attempted": False, "previous_image": None, "policy": None, "gate_recheck": None,
              "started_at": _now(), "finished_at": None}
    if not _create(receipts_dir, record):
        return get_desired_result(application_id, receipts_dir)

    def finish(status, reason, *, mutation=False, policy=None, previous_image=None):
        if status in ("APPLIED", "ALREADY_CLOSED"):
            recheck = {"application_id": application_id, **recheck_fields, "tests": list(POLICY_GATE_RECHECK_TESTS)}
            if plan_id:
                recheck["plan_id"] = plan_id
            record["gate_recheck"] = recheck
        record.update(status=status, reason=reason, cluster_mutation_attempted=mutation, policy=policy,
                      previous_image=previous_image, finished_at=_now())
        _replace(receipts_dir, record)
        return record

    # From here on the record exists: every outcome is recorded, never raised, so a caller that already
    # changed something (ADR-0044: the image, in the same T31 application) never sees STARTED forever.
    try:
        t9, renderer_sha256 = load_t9()
    except PolicyRefused as exc:
        return finish("REFUSED", exc.code)
    try:
        current = _secret_bytes(kube.get_secret(target.namespace, target.secret), target.secret_key)
    except Exception:  # noqa: BLE001 - no detail: the Secret's content must not reach a record
        return finish("REFUSED", "CLUSTER_READ_FAILED")
    try:
        render_closed(t9, current, target.bucket)  # the target is in a shape T9 renders
    except PolicyRefused as exc:
        return finish("REFUSED", exc.code)
    if not any(i.get("name") == "anonymous" for i in json.loads(current)["identities"]):
        observed = {"state": "closed", "renderer_sha256": renderer_sha256,
                    "previous_s3_json_sha256": hashlib.sha256(current).hexdigest()}
        return finish("ALREADY_CLOSED", "NO_ANONYMOUS_IDENTITY", policy=observed)
    try:
        result = apply_bucket_policy(kube, target, t9=t9, renderer_sha256=renderer_sha256,
                                     application_id=application_id, timeout_seconds=timeout_seconds,
                                     **(policy_hooks or {}))
    except DeployMismatch as exc:
        if not exc.mutation_attempted:
            return finish("REFUSED", exc.code, policy=exc.policy)
        return finish("SAFE_HOLD", exc.code, mutation=True, policy=exc.policy, previous_image=exc.previous_image)
    return finish("APPLIED", applied, mutation=True, policy=result["policy"], previous_image=result["previous_image"])
