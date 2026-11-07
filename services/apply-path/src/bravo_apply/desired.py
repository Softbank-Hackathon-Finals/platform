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
3. One record per ``application_id`` (``iac-<environment>-<revision[:16]>``), created before the
   cluster is touched. A second call returns that record and changes nothing.
4. A target already without the ``anonymous`` identity is left as it is (``ALREADY_CLOSED``).

Secret values never leave ``policy.py``: records carry sha256 digests of ``s3.json`` only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

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


class DesiredRefused(ValueError):
    """The request or the desired state is not one T31 applies; nothing was recorded or changed."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, timeout=60, check=False)


def _module(repo_root: Path, ref: str, environment_id: str) -> tuple[dict, str]:
    """The exposure module's params at ``ref`` and the sha256 of the environment file there."""
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
    return found[0]["params"], hashlib.sha256(shown.stdout).hexdigest()


def read_desired(repo_root: Path, environment_id: str, revision: str, reviewed_ref: str, bucket: str) -> dict:
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
    params, file_sha256 = _module(repo_root, revision, environment_id)
    if params.get("state") == "open-demo":
        raise DesiredRefused("T31_NEVER_OPENS")
    if params.get("state") != "closed" or set(params) != {"state", "bucket"}:
        raise DesiredRefused("DESIRED_STATE_UNRECOGNIZED")
    if params["bucket"] != bucket:
        raise DesiredRefused("DESIRED_BUCKET_MISMATCH")
    if _module(repo_root, reviewed_ref, environment_id)[0] != params:
        raise DesiredRefused("DESIRED_STATE_SUPERSEDED")
    return {"environment_id": environment_id, "revision": revision, "reviewed_ref": reviewed_ref,
            "module": MODULE, "module_version": MODULE_VERSION, "state": "closed", "bucket": bucket,
            "environment_file_sha256": file_sha256}


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


def apply_desired_exposure(environment_id: str, revision: str, *, reviewed_ref: str, repo_root: Path, kube: Kube,
                           receipts_dir: Path, target: PolicyTarget, plan_id: str | None = None,
                           timeout_seconds: int = 300, policy_hooks: dict | None = None) -> dict:
    """Make the target's ``s3.json`` match a reviewed ``closed`` desired state, then request IAM-01.1.

    Raises ``DesiredRefused`` before anything is recorded when Git does not ask for this change.
    Otherwise returns the record: ``APPLIED``, ``ALREADY_CLOSED``, ``REFUSED`` (cluster untouched)
    or ``SAFE_HOLD`` (the Secret may have changed; a person looks, nothing is rolled back)."""
    desired = read_desired(Path(repo_root), environment_id, revision, reviewed_ref, target.bucket)
    application_id = application_id_for(environment_id, revision)
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
            recheck = {"application_id": application_id, "environment_id": environment_id, "revision": revision,
                       "tests": list(POLICY_GATE_RECHECK_TESTS)}
            if plan_id:
                recheck["plan_id"] = plan_id
            record["gate_recheck"] = recheck
        record.update(status=status, reason=reason, cluster_mutation_attempted=mutation, policy=policy,
                      previous_image=previous_image, finished_at=_now())
        _replace(receipts_dir, record)
        return record

    t9, renderer_sha256 = load_t9()
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
        applied = apply_bucket_policy(kube, target, t9=t9, renderer_sha256=renderer_sha256,
                                      application_id=application_id, timeout_seconds=timeout_seconds,
                                      **(policy_hooks or {}))
    except DeployMismatch as exc:
        if not exc.mutation_attempted:
            return finish("REFUSED", exc.code, policy=exc.policy)
        return finish("SAFE_HOLD", exc.code, mutation=True, policy=exc.policy, previous_image=exc.previous_image)
    return finish("APPLIED", "DESIRED_STATE_APPLIED", mutation=True, policy=applied["policy"],
                  previous_image=applied["previous_image"])
