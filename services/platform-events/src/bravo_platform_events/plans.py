"""Plan read model and one guarded transition shared by storage backends."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from .contract import validate_identifier
from .store import EventError, EventStore


@dataclass(frozen=True)
class PlanSpec:
    """Immutable server-owned references, never an HTTP registration body."""

    plan_id: str
    tenant_id: str
    workload_id: str
    source_environment_id: str
    target_environment_id: str
    plan_digest: str
    collection_scope_digest: str


@dataclass(frozen=True)
class CollectionProof:
    """Output of a trusted collector, bound to the registered collection scope.

    Constructing this object is not proof verification. The host must obtain it
    from its collector/evidence store, not deserialize a client's assertion.
    """

    plan_id: str
    plan_digest: str
    collection_scope_digest: str
    evidence_id: str
    complete: bool


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("invalid_digest")


class PlanStore(EventStore):
    def __init__(self, path):
        super().__init__(path)
        with self._connection() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY REFERENCES streams(plan_id),
                    spec_json TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    state TEXT NOT NULL, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plan_commands (
                    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
                    command_key TEXT NOT NULL, request_json TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    PRIMARY KEY (plan_id, command_key)
                );
            """)

    def register_plan(self, spec: PlanSpec, *, run_id: str, occurred_at: str) -> None:
        """Trusted fixtures/registration only; same immutable spec is a no-op."""
        values = asdict(spec)
        for key, value in values.items():
            if key.endswith("_digest"):
                _digest(value)
            else:
                validate_identifier(value)
        canonical = _canonical(values)
        with self._transaction(write=True, plan_id=spec.plan_id) as c:
            old = c.execute(
                "SELECT spec_json FROM plans WHERE plan_id=?", (spec.plan_id,)
            ).fetchone()
            if old:
                if old["spec_json"] != canonical:
                    raise EventError("plan_conflict")
                return
            if c.execute("SELECT 1 FROM streams WHERE plan_id=?", (spec.plan_id,)).fetchone():
                raise EventError("stream_conflict")
            c.execute("INSERT INTO streams(plan_id) VALUES (?)", (spec.plan_id,))
            c.execute("INSERT INTO state_owners(plan_id) VALUES (?)", (spec.plan_id,))
            c.execute(
                "INSERT INTO plans VALUES (?,?,?,'COLLECT',1,?,?)",
                (spec.plan_id, canonical, spec.tenant_id, occurred_at, occurred_at),
            )
            self._append_in_transaction(
                c,
                plan_id=spec.plan_id,
                run_id=run_id,
                event_type="state.changed",
                data={"from": None, "to": "COLLECT"},
                occurred_at=occurred_at,
                producer_key="plan-created",
                state_owner=True,
            )

    def complete_collection(
        self,
        proof: CollectionProof,
        *,
        expected_version: int,
        command_key: str,
        run_id: str,
        occurred_at: str,
    ) -> dict:
        """The only transition: COLLECT -> ANALYZE after trusted collection proof.

        A retry returns the original receipt, not the current plan snapshot.
        All inputs, including the original timestamp/run, must remain identical.
        """
        validate_identifier(proof.plan_id)
        validate_identifier(proof.evidence_id)
        validate_identifier(command_key)
        validate_identifier(run_id)
        _digest(proof.plan_digest)
        _digest(proof.collection_scope_digest)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("invalid_version")
        if proof.complete is not True:
            raise EventError("collection_incomplete")
        request = _canonical(
            dict(
                proof=asdict(proof),
                expected_version=expected_version,
                run_id=run_id,
                occurred_at=occurred_at,
            )
        )
        with self._transaction(write=True, plan_id=proof.plan_id) as c:
            old = c.execute(
                "SELECT * FROM plan_commands WHERE plan_id=? AND command_key=?",
                (proof.plan_id, command_key),
            ).fetchone()
            if old:
                if old["request_json"] != request:
                    raise EventError("command_key_conflict")
                return json.loads(old["receipt_json"])
            plan = c.execute("SELECT * FROM plans WHERE plan_id=?", (proof.plan_id,)).fetchone()
            if plan is None:
                raise EventError("plan_not_found")
            spec = json.loads(plan["spec_json"])
            if any(
                getattr(proof, key) != spec[key]
                for key in ("plan_digest", "collection_scope_digest")
            ):
                raise EventError("collection_context_mismatch")
            if plan["version"] != expected_version:
                raise EventError("version_conflict")
            if plan["state"] != "COLLECT":
                raise EventError("transition_not_allowed")
            c.execute(
                "UPDATE plans SET state='ANALYZE',version=version+1,updated_at=? WHERE plan_id=?",
                (occurred_at, proof.plan_id),
            )
            event = self._append_in_transaction(
                c,
                plan_id=proof.plan_id,
                run_id=run_id,
                event_type="state.changed",
                data={"from": "COLLECT", "to": "ANALYZE"},
                occurred_at=occurred_at,
                producer_key=hashlib.sha256(f"collection:{command_key}".encode()).hexdigest(),
                state_owner=True,
            )
            receipt = dict(
                plan_id=proof.plan_id,
                state="ANALYZE",
                version=expected_version + 1,
                event_id=event["event_id"],
            )
            c.execute(
                "INSERT INTO plan_commands VALUES (?,?,?,?)",
                (proof.plan_id, command_key, request, _canonical(receipt)),
            )
            return receipt

    def tenant_for(self, plan_id: str) -> str | None:
        validate_identifier(plan_id)
        with self._transaction() as c:
            row = c.execute("SELECT tenant_id FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            return row["tenant_id"] if row else None

    def snapshot(self, plan_id: str) -> dict:
        validate_identifier(plan_id)
        with self._transaction() as c:
            plan = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise EventError("plan_not_found")
            return self._snapshot(c, plan)

    @staticmethod
    def _snapshot(c, plan) -> dict:
        sequence = (
            c.execute(
                "SELECT next_sequence FROM streams WHERE plan_id=?", (plan["plan_id"],)
            ).fetchone()["next_sequence"]
            - 1
        )
        spec = json.loads(plan["spec_json"])
        spec.pop("tenant_id")  # Authorization metadata is not part of the public read model.
        return dict(
            schema_version="1.0",
            **spec,
            state=plan["state"],
            version=plan["version"],
            created_at=plan["created_at"],
            updated_at=plan["updated_at"],
            last_event_id=f"{plan['plan_id']}:{sequence}",
        )
