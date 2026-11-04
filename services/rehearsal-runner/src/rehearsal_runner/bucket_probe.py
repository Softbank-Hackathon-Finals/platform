"""Bounded object-read observations for a future isolated verifier Job.

The caller supplies trusted clients, a test object and its expected digest. This
module never creates or deletes buckets, applies policies, or grants approval.
"""

import hashlib
from dataclasses import dataclass
from typing import Protocol


class ObjectStream(Protocol):
    def read(self, size: int) -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ReadResult:
    status: int
    body: ObjectStream | None = None


class ObjectReader(Protocol):
    def get(self, bucket: str, key: str) -> ReadResult: ...


@dataclass(frozen=True)
class BucketObservations:
    authenticated_before: str
    anonymous: str
    authenticated_after: str


def _known_object(reader: ObjectReader, bucket: str, key: str, expected_sha256: str, max_bytes: int) -> str:
    result = reader.get(bucket, key)
    try:
        if result.status != 200 or result.body is None:
            return "unknown"
        body = result.body.read(max_bytes + 1)
        if not isinstance(body, bytes) or len(body) > max_bytes:
            return "unknown"
        return "matches" if hashlib.sha256(body).hexdigest() == expected_sha256 else "unknown"
    finally:
        if result.body is not None:
            result.body.close()


def _anonymous(reader: ObjectReader, bucket: str, key: str) -> str:
    result = reader.get(bucket, key)
    try:
        if result.status == 403:
            return "denied"
        if result.status == 200:
            return "allowed"
        return "unknown"
    finally:
        if result.body is not None:
            result.body.close()


def observe_bucket_reads(
    authenticated: ObjectReader,
    anonymous: ObjectReader,
    *,
    bucket: str,
    key: str,
    expected_sha256: str,
    max_bytes: int = 4096,
) -> BucketObservations:
    """Bracket an ordinary anonymous GET with authenticated known-object reads.

    Observations are not a verification receipt. An authenticated failure, drift,
    transport error, or ambiguous denial cannot establish a successful test.
    """
    if not bucket or not key or len(expected_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in expected_sha256
    ) or not 1 <= max_bytes <= 65536:
        raise ValueError("invalid test object")
    try:
        before = _known_object(authenticated, bucket, key, expected_sha256, max_bytes)
        negative = _anonymous(anonymous, bucket, key)
        after = _known_object(authenticated, bucket, key, expected_sha256, max_bytes)
    except Exception:
        return BucketObservations("unknown", "unknown", "unknown")
    return BucketObservations(before, negative, after)
