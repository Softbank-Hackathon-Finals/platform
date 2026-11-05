"""Prove that a retained audit object cannot be deleted, shortened or overwritten by the
identity the platform runs with. Only error codes and HTTP statuses are recorded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


def _outcome(operation) -> str:
    """Only an authorisation refusal (403 / AccessDenied) counts as DENIED. A malformed request,
    a missing object or an unsupported method (400 / 404 / 405) proves nothing about protection
    and stays UNKNOWN so it can never be counted as evidence."""
    try:
        operation()
    except Exception as exc:  # noqa: BLE001 - boto ClientError or a fake; we only read its code
        response = getattr(exc, "response", None) or {}
        code = str(response.get("Error", {}).get("Code", type(exc).__name__))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code == "AccessDenied" or status == 403:
            return "DENIED"
        if code == "PreconditionFailed" or status == 412:
            return "PRECONDITION_FAILED"
        return f"UNKNOWN:{code}"
    return "ALLOWED"


def probe_denial(client: Any, bucket: str, key: str, version_id: str, *, now=None) -> dict:
    """Run the deletion/alteration attempts a tampering operator would try. ``pass`` requires every
    destructive attempt to be denied and the overwrite to fail its precondition."""
    now = now or (lambda: datetime.now(UTC))
    checks = {
        "delete_current_denied": _outcome(lambda: client.delete_object(Bucket=bucket, Key=key)),
        "delete_version_denied": _outcome(lambda: client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)),
        "shorten_retention_denied": _outcome(lambda: client.put_object_retention(
            Bucket=bucket, Key=key, VersionId=version_id,
            Retention={"Mode": "COMPLIANCE", "RetainUntilDate": now() + timedelta(minutes=1)})),
        "overwrite_precondition_failed": _outcome(lambda: client.put_object(
            Bucket=bucket, Key=key, Body=b"{}", IfNoneMatch="*")),
        # Review #257 P2: the 412 above only shows a *conditional* rewrite fails. A writer that
        # omits the header must be refused by the bucket itself. Probed on a sibling key that is
        # not a record (no ``.json``), so a misconfigured bucket gains a stray object, never a
        # second version of a record.
        "unconditional_write_denied": _outcome(lambda: client.put_object(
            Bucket=bucket, Key=f"{key.rsplit('/', 1)[0]}/.probe-unconditional-write", Body=b"{}")),
        "bucket_policy_change_denied": _outcome(lambda: client.put_bucket_policy(Bucket=bucket, Policy="{}")),
        "object_lock_config_change_denied": _outcome(lambda: client.put_object_lock_configuration(
            Bucket=bucket, ObjectLockConfiguration={"ObjectLockEnabled": "Enabled"})),
    }
    expected = {name: ("PRECONDITION_FAILED" if name == "overwrite_precondition_failed" else "DENIED") for name in checks}
    retention = "UNKNOWN"
    try:
        got = client.get_object_retention(Bucket=bucket, Key=key, VersionId=version_id).get("Retention", {})
        until = got.get("RetainUntilDate")
        retention = "COMPLIANCE_ACTIVE" if got.get("Mode") == "COMPLIANCE" and until is not None and until > now() else "NOT_RETAINED"
    except Exception:  # noqa: BLE001 - a write-only identity may not read retention; that is UNKNOWN, not PASS
        retention = "UNKNOWN"
    # PASS needs every attempt refused *and* a confirmed active COMPLIANCE retention. Unknown
    # retention (unreadable, no lock configuration) is not evidence of protection.
    passed = all(checks[name] == expected[name] for name in checks) and retention == "COMPLIANCE_ACTIVE"
    return {"bucket_ref": "redacted", "key_suffix": key.rsplit("/", 1)[-1], "checks": checks,
            "retention": retention, "pass": passed, "raw_output_suppressed": True}
