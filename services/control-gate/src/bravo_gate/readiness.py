"""Pre-cutover data readiness, never final data equality or an execution permit.

The host authenticates/pins producer records. This module neither connects to a
database nor chooses a T13 hashing implementation. Rules are selected server-side.
"""

from dataclasses import asdict, dataclass
from datetime import timedelta

from .contract import GateError, fingerprint, time
from .models import digest, identity


@dataclass(frozen=True)
class ReadinessPolicy:
    version: str
    schema_rule: str
    tables: tuple[str, ...]
    sequence_columns: tuple[tuple[str, str], ...]
    max_lag_bytes: int
    max_age_seconds: int

    def __post_init__(self):
        identity(self.version)
        identity(self.schema_rule)
        if (type(self.tables) is not tuple or not 1 <= len(self.tables) <= 64
                or len(set(self.tables)) != len(self.tables)):
            raise GateError("READINESS_TABLE_SCOPE_REQUIRED")
        for table in self.tables:
            identity(table)
            if len(table.split(".")) != 2 or any(not part for part in table.split(".")):
                raise GateError("QUALIFIED_TABLE_REQUIRED")
        if (type(self.sequence_columns) is not tuple
                or any(type(pair) is not tuple or len(pair) != 2 for pair in self.sequence_columns)
                or len(set(self.sequence_columns)) != len(self.sequence_columns)):
            raise GateError("INVALID_SEQUENCE_SCOPE")
        for table, column in self.sequence_columns:
            if table not in self.tables:
                raise GateError("SEQUENCE_OUTSIDE_SCOPE")
            identity(column)
        if (type(self.max_lag_bytes) is not int or not 0 <= self.max_lag_bytes <= 2**63 - 1
                or type(self.max_age_seconds) is not int or not 1 <= self.max_age_seconds <= 300):
            raise GateError("INVALID_READINESS_LIMIT")

    @property
    def fingerprint(self):
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class ReadinessBinding:
    context_digest: str
    policy: ReadinessPolicy
    source_database_ref: str
    target_database_ref: str
    snapshot_ref: str
    snapshot_digest: str

    def __post_init__(self):
        digest(self.context_digest)
        digest(self.snapshot_digest)
        if not isinstance(self.policy, ReadinessPolicy):
            raise GateError("SERVER_READINESS_POLICY_REQUIRED")
        for value in (self.source_database_ref, self.target_database_ref, self.snapshot_ref):
            identity(value)
        if self.source_database_ref == self.target_database_ref:
            raise GateError("DATABASE_PAIR_ALIAS")

    @property
    def fingerprint(self):
        return fingerprint(asdict(self))


def _keys(value, fields):
    if type(value) is not dict or set(value) != set(fields.split()):
        raise GateError("READINESS_SHAPE_INVALID")


def _int(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**63 - 1


def assess_readiness(ctx, binding, snapshot, *, now):
    """Pure comparison of already authenticated immutable records.

No caller-provided PASS flag is accepted. Missing/unknown observations block.
Schema mismatch is not waived by a security exception. Sequence checks qualify
a rehearsal for increasing, non-cycling sequences; T12 must repeat at cutover.
"""
    if (binding.context_digest != ctx.fingerprint or snapshot.ref != binding.snapshot_ref
            or snapshot.fingerprint != binding.snapshot_digest):
        raise GateError("READINESS_BINDING_CHANGED")
    doc, policy = snapshot.document, binding.policy
    _keys(doc, "kind context_digest policy_digest source target replication sequences")
    if (doc["kind"] != "data-readiness.v1" or doc["context_digest"] != ctx.fingerprint
            or doc["policy_digest"] != policy.fingerprint):
        raise GateError("READINESS_CONTEXT_MISMATCH")
    rows, limits = [], []

    def row(test_id, reason):
        rows.append({"test_id": test_id, "decision": "PASS" if reason == "READY" else "BLOCK",
                     "reason": reason})

    def fresh(record):
        observed, expires = time(record["observed_at"]), time(record["expires_at"])
        limit = min(expires, observed + timedelta(seconds=policy.max_age_seconds))
        limits.append(limit)
        return observed <= now < limit

    indexed = {}
    for side in ("source", "target"):
        part = doc[side]
        _keys(part, "environment_id environment_ref database_ref schema_rule observed_at expires_at tables")
        if (part["environment_id"] != getattr(ctx, side + "_environment_id")
                or part["environment_ref"] != getattr(ctx, side + "_environment_ref")
                or part["database_ref"] != getattr(binding, side + "_database_ref")):
            raise GateError("READINESS_RESOURCE_MISMATCH")
        row(side + ":freshness", "READY" if fresh(part) else "STALE_OR_FUTURE")
        row(side + ":schema-rule", "READY" if part["schema_rule"] == policy.schema_rule else "RULE_MISMATCH")
        if type(part["tables"]) is not list or len(part["tables"]) > 64:
            raise GateError("READINESS_TABLE_LIMIT")
        tables = {}
        for table in part["tables"]:
            _keys(table, "name schema_hash primary_key")
            identity(table["name"])
            if table["name"] in tables or table["name"] not in policy.tables:
                raise GateError("DUPLICATE_OR_UNREGISTERED_TABLE")
            if table["schema_hash"] is not None:
                digest(table["schema_hash"])
            pk = table["primary_key"]
            if pk is not None:
                if type(pk) is not list or len(pk) > 64 or any(type(k) is not str for k in pk):
                    raise GateError("INVALID_PRIMARY_KEY")
                for column in pk:
                    identity(column)
                if len(set(pk)) != len(pk):
                    raise GateError("INVALID_PRIMARY_KEY")
            tables[table["name"]] = table
        indexed[side] = tables
    for name in policy.tables:
        a, b = (indexed[side].get(name) for side in ("source", "target"))
        if a is None or b is None:
            row(name + ":schema", "MISSING_TABLE")
            row(name + ":pk", "MISSING_TABLE")
            continue
        schema = ("UNKNOWN" if a["schema_hash"] is None or b["schema_hash"] is None else
                  "READY" if a["schema_hash"] == b["schema_hash"] else "SCHEMA_MISMATCH")
        row(name + ":schema", schema)
        row(name + ":pk", "READY" if a["primary_key"] and a["primary_key"] == b["primary_key"] else "PK_NOT_READY")
    rep = doc["replication"]
    _keys(rep, "source_database_ref target_database_ref observed_at expires_at lag_bytes")
    if any(rep[side + "_database_ref"] != getattr(binding, side + "_database_ref")
           for side in ("source", "target")):
        raise GateError("REPLICATION_DIRECTION_MISMATCH")
    row("replication:freshness", "READY" if fresh(rep) else "STALE_OR_FUTURE")
    lag = rep["lag_bytes"]
    row("replication:lag", "UNKNOWN" if not _int(lag) else
        "READY" if lag <= policy.max_lag_bytes else "LAG_EXCEEDED")
    sequences = doc["sequences"]
    if type(sequences) is not list or len(sequences) > 128:
        raise GateError("SEQUENCE_LIMIT")
    seen = set()
    for seq in sequences:
        _keys(seq, "table column source_next target_next target_max_id proposed_next readback_next increment cycle observed_at expires_at")
        identity(seq["table"])
        identity(seq["column"])
        key = (seq["table"], seq["column"])
        if key in seen or key not in policy.sequence_columns:
            raise GateError("DUPLICATE_OR_UNREGISTERED_SEQUENCE")
        seen.add(key)
        prefix = ":".join(key)
        row(prefix + ":freshness", "READY" if fresh(seq) else "STALE_OR_FUTURE")
        values = [seq[k] for k in ("source_next", "target_next", "proposed_next", "readback_next")]
        maximum = seq["target_max_id"]
        # NULL max_id means an empty table; unknown observations must not use NULL
        # here and must instead leave a next/readback observation unknown.
        known = all(_int(v, 1) for v in values) and (maximum is None or _int(maximum))
        if not known:
            reason = "UNKNOWN"
        elif type(seq["increment"]) is not int or seq["increment"] != 1 or seq["cycle"] is not False:
            reason = "UNSUPPORTED_SEQUENCE"
        else:
            lower = max(values[0], values[1], (maximum + 1) if maximum is not None else 1)
            reason = "READY" if values[2] >= lower and values[3] == values[2] else "SEQUENCE_REHEARSAL_FAILED"
        row(prefix + ":sequence", reason)
    for key in policy.sequence_columns:
        if key not in seen:
            row(":".join(key) + ":sequence", "MISSING_SEQUENCE")
    return {"kind": "data-readiness-result.v1", "binding_digest": binding.fingerprint,
            "snapshot_digest": snapshot.fingerprint, "policy_digest": policy.fingerprint,
            "decision": "PASS" if all(r["decision"] == "PASS" for r in rows) else "BLOCK",
            "rows": rows, "valid_until": min(limits).isoformat()}
