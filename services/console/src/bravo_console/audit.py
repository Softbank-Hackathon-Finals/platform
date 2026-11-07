"""Read a host-registered T24 snapshot; never trust a hash chain as provenance.

T24 owns the writer and chain verifier. The host binds an immutable snapshot to
its authorized plan/run and authenticates that registration on every read.
"""

import asyncio
import re
from dataclasses import dataclass

from .observations import ObservationError, _plan
from .wire import object_document

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
HASH = re.compile(r"[a-f0-9]{64}")
TOOLS = frozenset(
    {
        "rehearsal.exec",
        "unlisted",
        "policy.evaluate",
        "plan.propose",
        "repo.read",
        "inventory.describe",
        "rehearsal.run",
        "rehearsal.result",
        "model",
    }
)


@dataclass(frozen=True, repr=False)
class RegisteredAuditSnapshot:
    plan_id: str
    plan_digest: str
    run_id: str
    record_count: int
    chain_digest: str
    document: bytes  # {"records": [...]}; no user-upload endpoint


class PlanAudit:
    def __init__(self, api, *, load, authenticate, verify_chain):
        if not all(callable(f) for f in (load, authenticate, verify_chain)):
            raise ValueError("explicit_audit_authorities_required")
        self.api, self.load, self.authenticate = api, load, authenticate
        self.verify_chain = verify_chain

    def _project(self, registered, plan):
        if (
            not isinstance(registered, RegisteredAuditSnapshot)
            or registered.plan_id != plan["plan_id"]
            or registered.plan_digest != plan["plan_digest"]
            or not ID.fullmatch(registered.run_id)
            or type(registered.record_count) is not int
            or not 1 <= registered.record_count <= 512
            or not HASH.fullmatch(registered.chain_digest)
            or type(registered.document) is not bytes
        ):
            raise ValueError("invalid_registered_audit")
        records = object_document(registered.document, max_bytes=2097152)["records"]
        if type(records) is not list or len(records) != registered.record_count:
            raise ValueError("incomplete_registered_snapshot")
        ok, _ = self.verify_chain(records)  # actual T24 schema + hash verifier
        if ok is not True or records[-1]["sha256"] != registered.chain_digest:
            raise ValueError("invalid_audit_chain")
        origins, rows = set(), []
        for record in records:
            if (record["plan_id"], record["run_id"]) != (plan["plan_id"], registered.run_id):
                raise ValueError("foreign_audit_run")
            if record["origin_id"] in origins:
                raise ValueError("duplicate_audit_origin")
            origins.add(record["origin_id"])
            if record["source"] != "agent.tools" or record["kind"] != "DESTRUCTIVE_ATTEMPT_BLOCKED":
                continue
            payload = record["payload"]
            if payload.get("category") != "bucket_delete":
                continue
            attempt = payload.get("attempt")
            if (
                payload.get("outcome") != "DENIED"
                or payload.get("reason") != record["kind"]
                or type(attempt) is not int
                or not 1 <= attempt <= 1000000
                or payload.get("tool") not in TOOLS
                or not re.fullmatch(
                    rf"t11:{re.escape(registered.run_id)}:attempt{attempt}:[0-9]+",
                    record["origin_id"],
                )
            ):
                raise ValueError("inconsistent_tool_denial")
            # Deliberately no raw payload, command, arbitrary message, URL or key.
            rows.append(
                {
                    "record_id": record["record_id"],
                    "recorded_at": record["recorded_at"],
                    "origin_id": record["origin_id"],
                    "attempt": attempt,
                    "tool": payload["tool"],
                    "outcome": "DENIED",
                    "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED",
                    "category": "bucket_delete",
                }
            )
        return {
            "kind": "console-agent-audit.v1",
            "plan_id": plan["plan_id"],
            "plan_digest": plan["plan_digest"],
            "run_id": registered.run_id,
            "record_count": registered.record_count,
            "chain_digest": registered.chain_digest,
            "authorizes_cutover": False,
            "retention_verified": False,
            "rows": rows,
        }

    async def read(self, scope, plan_id):
        try:
            before = await _plan(self.api, scope, plan_id)
            registered = await asyncio.wait_for(self.load(scope, dict(before)), 3)
            if (
                await asyncio.wait_for(self.authenticate(scope, dict(before), registered), 3)
                is not True
            ):
                raise ObservationError("audit_unverified")
            result = self._project(registered, before)
            after = await _plan(self.api, scope, plan_id)
            if (
                before != after
                or await asyncio.wait_for(self.authenticate(scope, dict(after), registered), 3)
                is not True
            ):
                raise ObservationError("audit_authority_changed")
            return result
        except ObservationError:
            raise
        except Exception:
            raise ObservationError("audit_unavailable") from None


def t24_reader(api, *, load, authenticate):
    """Composition hook: requires the separately installed, qualified T24 package.

    Missing package is a startup error, never a local replacement verifier.
    """
    from bravo_audit.ledger import verify_chain

    return PlanAudit(api, load=load, authenticate=authenticate, verify_chain=verify_chain)
