"""Operator-registered rehearsal tasks for T10's `RehearsalHost.task`.

Each registration is write-once and HMAC-signed with a key the trusted host
holds. It binds the plan ID, tenant and plan digest, so a changed plan stops
matching instead of reusing an old task. Until T16 inventory output is merged,
an operator registers the collected source S3 policy here.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .bucket_task import BucketPolicyTask, prepare_bucket_policy_task
from .secret_task import CredentialMigrationTask, prepare_credential_migration

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
KINDS = ("bucket-policy", "credential-migration")
PLACEHOLDER_RUN = "catalog"  # T15RehearsalHost replaces it with T10's rehearsal ID
# A 32 KiB policy can grow up to sixfold when JSON-escaped; reads and writes share this bound.
MAX_REGISTRATION_BYTES = 262144


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class CatalogUnavailable(RuntimeError):
    """No valid registration exists for this plan and task."""


DEFAULT_KINDS = {"source_kind": "aws", "target_kind": "onprem"}  # omitted from records: old MACs still verify


def build_task(kind: str, source_sha: str, source_policy: str | None = None, target_bucket: str | None = None,
               *, source_kind: str = "aws", target_kind: str = "onprem"):
    if kind == "bucket-policy":
        return prepare_bucket_policy_task(PLACEHOLDER_RUN, source_policy, target_bucket, source_sha=source_sha,
                                          source_kind=source_kind, target_kind=target_kind)
    if kind == "credential-migration":
        if source_policy is not None or target_bucket is not None:
            raise ValueError("credential task takes only a source revision")
        return prepare_credential_migration(PLACEHOLDER_RUN, source_sha, source_kind=source_kind,
                                            target_kind=target_kind)
    raise ValueError("unsupported task kind")


@dataclass(frozen=True)
class TaskCatalog:
    """Implements `rehearsal_host.TaskCatalog` from signed registrations under `root`."""

    root: Path
    key: bytes

    def __post_init__(self):
        if not isinstance(self.key, bytes) or len(self.key) < 32:
            raise ValueError("catalog key must be at least 32 bytes")

    def _path(self, plan_id: str, task_id: str) -> Path:
        if any(not isinstance(v, str) or _ID.fullmatch(v) is None for v in (plan_id, task_id)):
            raise ValueError("invalid plan or task ID")
        return Path(self.root) / plan_id / f"{task_id}.json"

    def _mac(self, value: dict) -> str:
        return hmac.new(self.key, _canonical(value), hashlib.sha256).hexdigest()

    def register(self, *, plan_id: str, tenant_id: str, plan_digest: str, task_id: str, kind: str,
                 source_sha: str, source_policy: str | None = None, target_bucket: str | None = None,
                 source_kind: str = "aws", target_kind: str = "onprem") -> dict:
        """Write one registration; an existing one is never replaced."""
        if _ID.fullmatch(tenant_id or "") is None or re.fullmatch(r"[a-f0-9]{64}", plan_digest or "") is None:
            raise ValueError("invalid plan binding")
        kinds = {"source_kind": source_kind, "target_kind": target_kind}
        build_task(kind, source_sha, source_policy, target_bucket, **kinds)
        record = {"plan_id": plan_id, "tenant_id": tenant_id, "plan_digest": plan_digest, "task_id": task_id,
                  "kind": kind, "source_sha": source_sha}
        if kind == "bucket-policy":
            record |= {"source_policy": source_policy, "target_bucket": target_bucket}
        if kinds != DEFAULT_KINDS:
            record |= kinds
        envelope = _canonical({"record": record, "mac": self._mac(record)})
        if len(envelope) > MAX_REGISTRATION_BYTES:
            raise ValueError("registration too large")
        path = self._path(plan_id, task_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            raise CatalogUnavailable("task already registered") from None
        with os.fdopen(fd, "wb") as stream:
            stream.write(envelope)
            stream.flush()
            os.fsync(stream.fileno())
        return record

    def _load(self, plan_id: str, task_id: str) -> dict:
        try:
            fd = os.open(self._path(plan_id, task_id), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise CatalogUnavailable("task not registered") from None
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(MAX_REGISTRATION_BYTES + 1)
        if len(raw) > MAX_REGISTRATION_BYTES:
            raise CatalogUnavailable("registration too large")
        try:
            outer = json.loads(raw)
            record = outer["record"]
            valid = set(outer) == {"record", "mac"} and hmac.compare_digest(outer["mac"], self._mac(record))
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid or record.get("plan_id") != plan_id or record.get("task_id") != task_id:
            raise CatalogUnavailable("registration signature or identity mismatch")
        return record

    @staticmethod
    def _task(record: dict) -> BucketPolicyTask | CredentialMigrationTask:
        return build_task(record["kind"], record["source_sha"], record.get("source_policy"),
                          record.get("target_bucket"),
                          **{k: record.get(k, v) for k, v in DEFAULT_KINDS.items()})

    def for_plan(self, connection: object, plan: dict, task_id: str):
        """Read in T10's plan transaction; the registration must match the current plan."""
        record = self._load(plan["plan_id"], task_id)
        spec = json.loads(plan["spec_json"])
        if record["tenant_id"] != plan["tenant_id"] or record["plan_digest"] != spec["plan_digest"]:
            raise CatalogUnavailable("registration belongs to another plan")
        return self._task(record)

    def for_admission(self, admission: object):
        """Same registration after commit; T15RehearsalHost re-checks the task digest."""
        record = self._load(admission.plan_id, admission.task_id)
        if record["tenant_id"] != admission.tenant_id:
            raise CatalogUnavailable("registration belongs to another tenant")
        return self._task(record)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Register one rehearsal task for a T10 plan (write-once)")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True, help="private file holding the catalog HMAC key")
    for name in ("plan-id", "tenant-id", "plan-digest", "task-id", "source-sha"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--source-policy-file", type=Path, help="collected source S3 policy (bucket-policy)")
    parser.add_argument("--target-bucket", help="dedicated rehearsal bucket (bucket-policy)")
    args = parser.parse_args(argv)
    if args.key_file.stat().st_mode & 0o077:
        parser.error("key file must not be readable by group or others")
    catalog = TaskCatalog(args.root, args.key_file.read_bytes().strip())
    policy = args.source_policy_file.read_text(encoding="utf-8") if args.source_policy_file else None
    try:
        record = catalog.register(plan_id=args.plan_id, tenant_id=args.tenant_id, plan_digest=args.plan_digest,
                                  task_id=args.task_id, kind=args.kind, source_sha=args.source_sha,
                                  source_policy=policy, target_bucket=args.target_bucket)
    except (ValueError, CatalogUnavailable) as error:
        print(f"registration refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps({k: record[k] for k in ("plan_id", "task_id", "kind", "source_sha")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
