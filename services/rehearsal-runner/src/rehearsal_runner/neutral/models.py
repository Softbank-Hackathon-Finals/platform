"""Neutral models of ADR-0034 (`docs/contracts/neutral-models.schema.json`).

Exporters turn a source policy into one of these; appliers render it for the target. The
documents never carry a secret value, a credential or an account identifier in clear.

Validation here is the schema plus two rules the schema cannot state: ``public_read`` and
``public_list`` must agree with the anonymous grants, and the order of actions is canonical so
the same policy always serialises to the same bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["aws", "gcp", "onprem"]
KINDS: tuple[str, ...] = ("aws", "gcp", "onprem")
ACTIONS: tuple[str, ...] = ("read", "write", "list", "delete")
OBJECT_SCHEMA = "bravo.neutral.object-access/v1"
SECRET_SCHEMA = "bravo.neutral.secret-binding/v1"

NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
SERVICE_ACCOUNT_RE = re.compile(r"^[a-z0-9]([-.a-z0-9]{0,251}[a-z0-9])?$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
LOGICAL_NAME_RE = NAMESPACE_RE
SECRET_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class NeutralError(ValueError):
    """A document or source policy that cannot be represented. ``code`` is an enum, never input."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise NeutralError(code)


def _exact_keys(doc: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    _require(isinstance(doc, dict) and required <= set(doc) <= required | optional, "SCHEMA_MISMATCH")
    return doc


@dataclass(frozen=True)
class Workload:
    namespace: str
    service_account: str

    def __post_init__(self):
        _require(isinstance(self.namespace, str) and NAMESPACE_RE.fullmatch(self.namespace) is not None
                 and isinstance(self.service_account, str)
                 and SERVICE_ACCOUNT_RE.fullmatch(self.service_account) is not None, "INVALID_WORKLOAD")

    def to_dict(self) -> dict:
        return {"kind": "workload", "namespace": self.namespace, "service_account": self.service_account}


@dataclass(frozen=True)
class Anonymous:
    def to_dict(self) -> dict:
        return {"kind": "anonymous"}


@dataclass(frozen=True)
class Unresolved:
    """A source principal the exporter could not map to a workload. Appliers never grant it."""

    ref: str

    def __post_init__(self):
        _require(isinstance(self.ref, str) and len(self.ref) <= 256, "INVALID_UNRESOLVED_REF")

    def to_dict(self) -> dict:
        return {"kind": "unresolved", "ref": self.ref}


Principal = Workload | Anonymous | Unresolved


def principal_from_dict(doc: object) -> Principal:
    kind = doc.get("kind") if isinstance(doc, dict) else None
    if kind == "workload":
        _exact_keys(doc, {"kind", "namespace", "service_account"})
        return Workload(doc["namespace"], doc["service_account"])
    if kind == "anonymous":
        _exact_keys(doc, {"kind"})
        return Anonymous()
    if kind == "unresolved":
        _exact_keys(doc, {"kind", "ref"})
        return Unresolved(doc["ref"])
    raise NeutralError("SCHEMA_MISMATCH")


@dataclass(frozen=True)
class SourceRef:
    kind: str
    ref: str  # masked identifier (bucket or secret name), never a value

    def __post_init__(self):
        _require(self.kind in KINDS and isinstance(self.ref, str) and len(self.ref) <= 512, "INVALID_SOURCE_REF")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "ref": self.ref}

    @classmethod
    def from_dict(cls, doc: object) -> SourceRef:
        _exact_keys(doc, {"kind", "ref"})
        return cls(doc["kind"], doc["ref"])


def canonical_actions(actions) -> tuple[str, ...]:
    _require(isinstance(actions, list | tuple | set | frozenset)
             and all(isinstance(a, str) for a in actions), "INVALID_ACTIONS")
    values = set(actions)
    _require(values and values <= set(ACTIONS), "INVALID_ACTIONS")
    return tuple(a for a in ACTIONS if a in values)


@dataclass(frozen=True)
class Grant:
    principal: Principal
    actions: tuple[str, ...]
    prefix: str = ""

    def __post_init__(self):
        _require(isinstance(self.principal, Workload | Anonymous | Unresolved), "INVALID_PRINCIPAL")
        _require(isinstance(self.actions, tuple) and self.actions == canonical_actions(self.actions)
                 and len(set(self.actions)) == len(self.actions), "INVALID_ACTIONS")
        _require(isinstance(self.prefix, str) and len(self.prefix) <= 256, "INVALID_PREFIX")

    def to_dict(self) -> dict:
        return {"principal": self.principal.to_dict(), "actions": list(self.actions), "prefix": self.prefix}

    @classmethod
    def from_dict(cls, doc: object) -> Grant:
        _exact_keys(doc, {"principal", "actions"}, {"prefix"})
        actions = doc["actions"]
        _require(isinstance(actions, list) and actions and len(set(actions)) == len(actions), "INVALID_ACTIONS")
        return cls(principal_from_dict(doc["principal"]), canonical_actions(actions), doc.get("prefix", ""))


def _digest(doc: dict) -> str:
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ObjectAccessPolicy:
    bucket: str
    source: SourceRef
    grants: tuple[Grant, ...]
    public_read: bool
    public_list: bool
    tls_required: bool

    def __post_init__(self):
        _require(isinstance(self.bucket, str) and BUCKET_RE.fullmatch(self.bucket) is not None, "INVALID_BUCKET")
        _require(isinstance(self.source, SourceRef), "INVALID_SOURCE_REF")
        _require(isinstance(self.grants, tuple) and len(self.grants) <= 64
                 and all(isinstance(g, Grant) for g in self.grants), "INVALID_GRANTS")
        _require(all(type(v) is bool for v in (self.public_read, self.public_list, self.tls_required)),
                 "SCHEMA_MISMATCH")
        anonymous = [g for g in self.grants if isinstance(g.principal, Anonymous)]
        _require(self.public_read == any("read" in g.actions for g in anonymous)
                 and self.public_list == any("list" in g.actions for g in anonymous), "PUBLIC_FLAGS_DISAGREE")

    @property
    def unresolved(self) -> tuple[str, ...]:
        return tuple(g.principal.ref for g in self.grants if isinstance(g.principal, Unresolved))

    def to_dict(self) -> dict:
        return {"schema": OBJECT_SCHEMA, "bucket": self.bucket, "source": self.source.to_dict(),
                "grants": [g.to_dict() for g in self.grants], "public_read": self.public_read,
                "public_list": self.public_list, "tls_required": self.tls_required}

    def digest(self) -> str:
        return _digest(self.to_dict())

    @classmethod
    def from_dict(cls, doc: object) -> ObjectAccessPolicy:
        _exact_keys(doc, {"schema", "bucket", "source", "grants", "public_read", "public_list", "tls_required"})
        _require(doc["schema"] == OBJECT_SCHEMA, "SCHEMA_MISMATCH")
        _require(isinstance(doc["grants"], list), "INVALID_GRANTS")
        return cls(doc["bucket"], SourceRef.from_dict(doc["source"]),
                   tuple(Grant.from_dict(g) for g in doc["grants"]),
                   doc["public_read"], doc["public_list"], doc["tls_required"])


@dataclass(frozen=True)
class SecretBinding:
    logical_name: str
    source: SourceRef
    keys: tuple[str, ...]
    consumers: tuple[Workload, ...]
    rotation_required: bool = False
    # Source principals that could not be mapped to a workload. Not part of the document: they are
    # reported so the item becomes agent_needed, and no applier grants them.
    unresolved: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self):
        _require(isinstance(self.logical_name, str) and LOGICAL_NAME_RE.fullmatch(self.logical_name) is not None,
                 "INVALID_LOGICAL_NAME")
        _require(isinstance(self.source, SourceRef), "INVALID_SOURCE_REF")
        _require(isinstance(self.keys, tuple) and 1 <= len(self.keys) <= 32 and len(set(self.keys)) == len(self.keys)
                 and all(isinstance(k, str) and SECRET_KEY_RE.fullmatch(k) for k in self.keys), "INVALID_KEYS")
        _require(isinstance(self.consumers, tuple) and 1 <= len(self.consumers) <= 16
                 and all(isinstance(c, Workload) for c in self.consumers), "INVALID_CONSUMERS")
        _require(type(self.rotation_required) is bool, "SCHEMA_MISMATCH")

    def to_dict(self) -> dict:
        return {"schema": SECRET_SCHEMA, "logical_name": self.logical_name, "source": self.source.to_dict(),
                "keys": list(self.keys), "consumers": [c.to_dict() for c in self.consumers],
                "rotation_required": self.rotation_required}

    def digest(self) -> str:
        return _digest(self.to_dict())

    @classmethod
    def from_dict(cls, doc: object) -> SecretBinding:
        _exact_keys(doc, {"schema", "logical_name", "source", "keys", "consumers"}, {"rotation_required"})
        _require(doc["schema"] == SECRET_SCHEMA, "SCHEMA_MISMATCH")
        _require(isinstance(doc["keys"], list) and isinstance(doc["consumers"], list), "SCHEMA_MISMATCH")
        consumers = []
        for c in doc["consumers"]:
            principal = principal_from_dict(c)
            _require(isinstance(principal, Workload), "INVALID_CONSUMERS")
            consumers.append(principal)
        return cls(doc["logical_name"], SourceRef.from_dict(doc["source"]), tuple(doc["keys"]),
                   tuple(consumers), doc.get("rotation_required", False))


def from_document(doc: object) -> ObjectAccessPolicy | SecretBinding:
    """Validate one neutral document (either model) and return it."""
    schema = doc.get("schema") if isinstance(doc, dict) else None
    if schema == OBJECT_SCHEMA:
        return ObjectAccessPolicy.from_dict(doc)
    if schema == SECRET_SCHEMA:
        return SecretBinding.from_dict(doc)
    raise NeutralError("SCHEMA_MISMATCH")
