"""T13 readiness records for T19 in T10's plan transaction: store, and serve the newest per context.

Storing a binding and its snapshot does not authenticate them. T19 still requires the registered
`readiness-binding` and `readiness-snapshot` signatures (PlanAuthorityRecords) at evaluation and at
consumption. This module only keeps the bytes the producer recorded and the observation time the
signature check needs, under the same plan lock as the rest of the gate records.
"""

import json
from dataclasses import asdict

from bravo_gate.contract import canonical, fingerprint
from bravo_gate.models import RunSnapshot
from bravo_gate.readiness import ReadinessBinding, ReadinessPolicy

from .plans import _canonical
from .store import EventError

PREFIX = "gate-readiness-"
MAX_RECORDS = 512  # per context; a new capture every 30-60 s fits a day of demo runs


def _prefix(context_digest):
    return f"{PREFIX}{context_digest}-"


def observed_at(snapshot_document):
    """The earliest component observation: every component must be newer than key enrollment."""
    doc = snapshot_document
    times = [doc[side]["observed_at"] for side in ("source", "target", "replication")]
    times += [row["observed_at"] for row in doc.get("sequences", [])]
    return min(times)


def record_readiness(records, principal, plan_id, binding, snapshot):
    """Append a T13 binding and its snapshot for the plan's current gate context (gate:record)."""
    if type(binding) is not ReadinessBinding or type(snapshot) is not RunSnapshot:
        raise EventError("gate_readiness_record_invalid")
    if snapshot.ref != binding.snapshot_ref or snapshot.fingerprint != binding.snapshot_digest:
        raise EventError("gate_readiness_binding_mismatch")
    with records.store._transaction(write=True, plan_id=plan_id) as c:
        plan = records._plan(c, principal, plan_id, "gate:record")
        view = records.view(c, principal, plan)
        if binding.context_digest != view.context.fingerprint:
            raise EventError("gate_readiness_context_mismatch")
        document = snapshot.document
        if document.get("context_digest") != binding.context_digest:
            raise EventError("gate_readiness_context_mismatch")
        value = {"binding": asdict(binding), "snapshot_ref": snapshot.ref, "snapshot": document,
                 "observed_at": observed_at(document)}
        prefix = _prefix(binding.context_digest)
        rows = c.execute("SELECT command_key, receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ? "
                         "ORDER BY command_key", (plan_id, prefix + "%")).fetchall()
        for row in rows:
            if json.loads(row["receipt_json"]) == json.loads(_canonical(value)):
                return value  # the same capture recorded again
        if len(rows) >= MAX_RECORDS:
            raise EventError("gate_readiness_record_limit")
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                  (plan_id, f"{prefix}{len(rows) + 1:06d}", "{}", _canonical(value)))
        return value


def _latest(view, context):
    row = view.c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ? "
                         "ORDER BY command_key DESC LIMIT 1",
                         (view.plan["plan_id"], _prefix(context.fingerprint) + "%")).fetchone()
    return json.loads(row["receipt_json"]) if row else None


def _binding(value):
    return binding_from_dict(value["binding"])


def binding_from_dict(raw):
    """A ReadinessBinding from its asdict() form (stored records, operator relay requests)."""
    policy = dict(raw["policy"])
    policy["tables"] = tuple(policy["tables"])
    policy["sequence_columns"] = tuple(tuple(pair) for pair in policy["sequence_columns"])
    return ReadinessBinding(raw["context_digest"], ReadinessPolicy(**policy), raw["source_database_ref"],
                            raw["target_database_ref"], raw["snapshot_ref"], raw["snapshot_digest"])


def binding_provider(view, actor, context):
    """StoredGateServices binding_provider: the newest recorded binding for this context, or None."""
    value = _latest(view, context)
    return None if value is None else _binding(value)


def snapshot_provider(view, actor, context, registered):
    """StoredGateServices snapshot_provider: the snapshot stored with `registered`, or None."""
    value = _latest(view, context)
    if value is None or _binding(value) != registered:
        return None
    snapshot = RunSnapshot(value["snapshot_ref"], canonical(value["snapshot"]))
    return snapshot if snapshot.fingerprint == fingerprint(value["snapshot"]) else None


def subject(view, purpose, subject_digest):
    """External subject metadata for readiness signatures, read from the stored record (or None)."""
    from bravo_gate.authority_records import AuthoritySubject

    if purpose not in ("readiness-binding", "readiness-snapshot"):
        return None
    value = _latest(view, view.context)
    if value is None:
        return None
    binding = _binding(value)
    expected = binding.fingerprint if purpose == "readiness-binding" else binding.snapshot_digest
    if subject_digest != expected:
        return None
    return AuthoritySubject(view.context.fingerprint, subject_digest, purpose, value["observed_at"])
