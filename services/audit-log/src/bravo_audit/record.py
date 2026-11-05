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


# A dictionary key that names a credential. Its string value must be a *reference marker*
# (where the value comes from), never the value itself. Names that only point at a secret
# (``secret_id``, ``secret_arn``, ``token_count``) are not credentials.
SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|[_.-])(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"credential|credentials|authorization)(?:$|[_.-])"
)
REFERENCE_SUFFIXES = ("_id", "_name", "_ref", "_arn", "_path", "_file", "_source", "_sources", "_kind",
                      "_mode", "_count", "_enabled", "_digest", "_sha256", "_hash", "_version", "_rotation")
REFERENCE_MARKERS = frozenset({"", "literal", "secretKeyRef", "configMapKeyRef", "fieldRef",
                               "resourceFieldRef", "unknown", "configurable", "redacted", "<redacted>"})
# ``<masked>`` or ``<masked: short reason>`` from the T16 scanner; nothing else that merely starts with "<".
MASK_MARKER = re.compile(r"^<masked(?:: [A-Za-z0-9 _-]{1,60})?>$")


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


class UnsafePayload(ValueError):
    """Payload contains a credential-like string or exceeds the size bound."""


def _reference_key(key: str) -> bool:
    """A name that only *points at* a secret (``secret_id``, ``password_ref``, ``token_count``)."""
    return key.lower().endswith(REFERENCE_SUFFIXES)


def _sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY.search(key)) and not _reference_key(key)


def _is_marker(value: str) -> bool:
    return value in REFERENCE_MARKERS or bool(MASK_MARKER.fullmatch(value))


def _check_structure(value, sensitive: bool = False, path: str = "") -> None:
    """Under a credential-named key, every string at any depth (lists and nested objects
    included) must be a reference marker. The sensitive context is inherited downward, except
    through a *reference-named* key: ``credentials: [{"secret_id": "demo/ref"}]`` and
    ``password: {"password_ref": "…"}`` carry pointers, not values (contract: reference names
    are exempt wherever they appear)."""
    if isinstance(value, dict):
        for key, item in value.items():
            key = str(key)
            child = False if _reference_key(key) else (sensitive or _sensitive_key(key))
            _check_structure(item, child, path + key + ".")
    elif isinstance(value, list | tuple):
        for item in value:
            _check_structure(item, sensitive, path + "[]")
    elif isinstance(value, str) and sensitive and not _is_marker(value):
        raise UnsafePayload(f"credential value under key path {path.rstrip('.')!r} refused")


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
    _check_structure(payload)
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
    origin_id: str
    payload: dict
    payload_sha256: str
    prev_sha256: str
    sha256: str

    def as_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def build(*, plan_id: str, run_id: str, sequence: int, recorded_at: str, source: str, kind: str,
              origin_id: str, payload: dict, prev_sha256: str) -> AuditRecord:
        body = {
            "schema_version": "1.0",
            "record_id": f"{run_id}:{sequence}",
            "plan_id": plan_id,
            "run_id": run_id,
            "sequence": sequence,
            "recorded_at": recorded_at,
            "source": source,
            "kind": kind,
            "origin_id": origin_id,
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
