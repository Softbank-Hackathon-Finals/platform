"""Audit record v1 (docs/contracts/T24-audit-record-v1-proposal.md).

A record wraps one upstream event (T10 plan event, T11 tool event, gate/executor result) and
binds it to its run with a hash chain. The writer never rewrites or reorders: ``sequence`` is
per run, ``prev_sha256`` is the previous record's ``sha256`` and ``sha256`` covers everything
else. Payload strings are refused if they look like credentials; the audit log is read by the
console and reviewers, never by the model, and must stay free of secrets.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from importlib import resources

import jsonschema

GENESIS = "0" * 64
MAX_PAYLOAD_BYTES = 16 * 1024
SECRET_LIKE = re.compile(
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|sk-(?:ant-)?[A-Za-z0-9_-]{16,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|://[^/\s@]+:[^/\s@]*@"
    r"|(?i:\b(?:password|passwd|api[_-]?key|access[_-]?token|secret[_-]?key)\b\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{8,})"
)


class UnsafePayload(ValueError):
    """Payload contains a credential-like string or exceeds the size bound."""


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


def check_payload(payload: dict) -> bytes:
    """Return the canonical bytes of a payload that is bounded and free of secret-like text."""
    if not isinstance(payload, dict):
        raise UnsafePayload("payload must be an object")
    data = canonical(payload)
    if len(data) > MAX_PAYLOAD_BYTES:
        raise UnsafePayload("payload exceeds 16 KiB")
    for text in _strings(payload):
        if "\x00" in text or SECRET_LIKE.search(text):
            raise UnsafePayload("payload contains a credential-like value")
    return data


@dataclass(frozen=True)
class AuditRecord:
    schema_version: str
    record_id: str
    plan_id: str
    run_id: str
    sequence: int
    recorded_at: str
    source: str
    kind: str
    payload: dict
    payload_sha256: str
    prev_sha256: str
    sha256: str

    def as_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def build(*, plan_id: str, run_id: str, sequence: int, recorded_at: str, source: str, kind: str,
              payload: dict, prev_sha256: str) -> AuditRecord:
        body = {
            "schema_version": "1.0",
            "record_id": f"{run_id}:{sequence}",
            "plan_id": plan_id,
            "run_id": run_id,
            "sequence": sequence,
            "recorded_at": recorded_at,
            "source": source,
            "kind": kind,
            "payload": payload,
            "payload_sha256": sha256(check_payload(payload)),
            "prev_sha256": prev_sha256,
        }
        record = AuditRecord(**body, sha256=sha256(canonical(body)))
        validate_record(record.as_dict())
        return record


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(resources.files("bravo_audit.schemas").joinpath("audit-record.schema.json").read_text("utf-8"))
    return jsonschema.Draft202012Validator(schema)


def validate_record(doc: dict) -> None:
    """Schema check plus the hash bindings. Raises ``jsonschema.ValidationError`` or ``ValueError``."""
    _validator().validate(doc)
    if doc["record_id"] != f"{doc['run_id']}:{doc['sequence']}":
        raise ValueError("record_id does not bind run_id and sequence")
    if doc["payload_sha256"] != sha256(canonical(doc["payload"])):
        raise ValueError("payload_sha256 mismatch")
    body = {k: v for k, v in doc.items() if k != "sha256"}
    if doc["sha256"] != sha256(canonical(body)):
        raise ValueError("sha256 mismatch")
