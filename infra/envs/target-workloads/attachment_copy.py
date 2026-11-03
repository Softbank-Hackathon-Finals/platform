#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3==1.43.123", "psycopg[binary]==3.3.6"]
# ///
"""Copy the quiescent attachment inventory once, just before cutover.

Reads attachments.object_key in a read-only transaction, then runs synchronize()
between the source and destination stores. Configuration comes only from the
environment. Output is one JSON receipt of counts and hashes; object names,
bodies, endpoints and credentials are never printed. Failure exits nonzero.
"""
import json
import os
from pathlib import Path

from sync_attachments import MAX_BYTES, MAX_OBJECTS, synchronize

# synchronize() and the stores raise these fixed strings. Any other message may carry a key or host.
PUBLIC_REASONS = frozenset({
    "approved extras must be distinct from database references",
    "bounded nonempty attachment inventory required",
    "source bucket and database attachment inventory differ",
    "destination contains unrelated objects; preserve and reconcile",
    "bounded binary source object required",
    "bounded transfer size exceeded",
    "destination conflict; existing object is preserved",
    "destination byte verification failed",
    "source changed during transfer; require quiescent retry",
    "final object inventory changed",
    "incomplete object read",
})
PUBLIC_S3_CODES = frozenset({
    "AccessDenied", "SignatureDoesNotMatch", "InvalidAccessKeyId", "ExpiredToken", "NoSuchBucket", "NoSuchKey",
    "InternalError", "ServiceUnavailable", "SlowDown", "RequestTimeout", "NotImplemented",
})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class AttachmentCopyFailed(Exception):
    """A failure whose fields and message are safe to log or return to the Cutover Executor."""

    def __init__(self, stage, reason, *, error_category=None, s3_error_code=None):
        super().__init__(f"attachment copy failed at {stage}: {reason}")
        self.stage = stage
        self.reason = reason
        self.error_category = error_category
        self.s3_error_code = s3_error_code

    def receipt(self):
        return {"passed": False, "stage": self.stage, "reason": self.reason,
                "error_category": self.error_category, "s3_error_code": self.s3_error_code,
                "raw_output_suppressed": True}


def describe_failure(stage, error):
    """Reduce an exception to public fields; its raw text can carry keys, hosts or a DSN."""
    reason = str(error) if isinstance(error, ValueError) and str(error) in PUBLIC_REASONS else "unclassified"
    response = getattr(error, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    if code is not None and code not in PUBLIC_S3_CODES:
        code = "unclassified"
    return AttachmentCopyFailed(stage, reason, error_category=type(error).__name__, s3_error_code=code)


def require_verified_tls(host, sslmode):
    if sslmode != "verify-full" and host not in LOOPBACK_HOSTS:
        raise AttachmentCopyFailed("database_inventory", "database connection must use verify-full TLS")


def connect_read_only(dsn=""):
    """An empty DSN uses the libpq PG* environment. Every transaction on it is READ ONLY."""
    import psycopg  # Default test discovery has no driver installed.
    connection = psycopg.connect(dsn, connect_timeout=10)
    try:
        require_verified_tls(connection.info.host, connection.info.get_parameters().get("sslmode"))
    except AttachmentCopyFailed:
        connection.close()
        raise
    connection.read_only = True
    return connection


def read_referenced_keys(connection):
    # One past the limit is enough for synchronize() to reject an oversized inventory.
    rows = connection.execute("SELECT DISTINCT object_key FROM public.attachments LIMIT %s",
                              (MAX_OBJECTS + 1,))
    return [row[0] for row in rows.fetchall()]


def copy_keys(source, destination, expected_keys, approved_extra_keys=()):
    # synchronize() learns the total only while copying; reject before the first destination write.
    if sum(source.sizes().values()) > MAX_BYTES:
        raise ValueError("bounded transfer size exceeded")
    result = synchronize(source, destination, expected_keys, approved_extra_keys=approved_extra_keys)
    # synchronize() hard-codes this field. Read-only is a property of the source store, tested
    # there; a receipt must not present a constant as a measurement.
    del result["source_write_calls"]
    return result


def approved_extras_from(environ):
    path = environ.get("ATTACHMENT_APPROVED_EXTRA_KEYS_FILE")
    if not path:
        return []
    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip()]


def stores_from(environ):
    def required(name):
        value = environ.get(name)
        if not value:
            raise AttachmentCopyFailed("configuration", "missing " + name)
        return value

    source_bucket = required("ATTACHMENT_SOURCE_BUCKET")
    region = required("ATTACHMENT_SOURCE_REGION")
    source_endpoint = environ.get("ATTACHMENT_SOURCE_ENDPOINT_URL") or None
    # Real S3 must prove the bucket belongs to the expected account; only a test endpoint may omit it.
    owner = environ.get("ATTACHMENT_SOURCE_BUCKET_OWNER") if source_endpoint else required(
        "ATTACHMENT_SOURCE_BUCKET_OWNER")
    target_endpoint = required("ATTACHMENT_TARGET_ENDPOINT_URL")
    target_bucket = required("ATTACHMENT_TARGET_BUCKET")
    access_key_id = required("ATTACHMENT_TARGET_ACCESS_KEY_ID")
    secret_access_key = required("ATTACHMENT_TARGET_SECRET_ACCESS_KEY")
    # The same store on both sides would reuse every object and report a copy that never happened.
    if (source_endpoint, source_bucket) == (target_endpoint, target_bucket):
        raise AttachmentCopyFailed("configuration", "source and destination are the same bucket")
    import attachment_stores  # Default test discovery has no SDK installed.
    return (attachment_stores.source_store(source_bucket, region, owner, source_endpoint),
            attachment_stores.destination_store(target_endpoint, target_bucket, access_key_id, secret_access_key))


def run_copy(environ=os.environ, *, db_dsn="", approved_extra_keys=None):
    """Run once and return the receipt, or raise AttachmentCopyFailed. Rerunning is safe."""
    stage = "configuration"
    try:
        extras = approved_extras_from(environ) if approved_extra_keys is None else approved_extra_keys
        source, destination = stores_from(environ)
        stage = "database_inventory"
        with connect_read_only(db_dsn) as connection:
            keys = read_referenced_keys(connection)
        stage = "transfer"
        return copy_keys(source, destination, keys, extras)
    except AttachmentCopyFailed:
        raise
    except Exception as error:  # noqa: BLE001 - every failure must leave as a sanitized receipt.
        raise describe_failure(stage, error) from None


def main():
    try:
        receipt = run_copy()
    except AttachmentCopyFailed as failure:
        print(json.dumps(failure.receipt()))
        return 1
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
