"""Read a host-registered T24 run; never trust a hash chain or a bucket listing as provenance.

T24 owns the writer, the version-aware reader (`read_run`) and the registration format. The host
keeps each run's registration (built from the writer's receipts) in its own append-only store,
binds it to the authorized plan/digest and authenticates it on every read.
"""

import asyncio
import re
from dataclasses import dataclass

from .observations import ObservationError, _plan

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
HASH = re.compile(r"[a-f0-9]{64}")
# T11 tool name rule (bravo_audit.adapters.TOOL_NAME). A tool outside it is shown as unknown on
# its own row; new T11 tools (sandbox.exec, ...) need no console change.
TOOL = re.compile(r"[a-z][a-z0-9_.]{0,63}")
MAX_RECORDS = 100_000
MAX_ROWS = 200  # newest rows shown; rows_total keeps the full count


@dataclass(frozen=True, repr=False)
class RegisteredAuditRun:
    plan_id: str
    plan_digest: str
    registration: dict  # T24 registration v1 from the host's append-only store, never the bucket
    anchors: tuple = ()  # earlier (record_count, chain_digest) the host accepted for this run


class PlanAudit:
    """`read(plan_id, run_id, registration)` returns T24's VerifiedRun (blocking; run in a thread)."""

    def __init__(self, api, *, load, authenticate, read):
        if not all(callable(f) for f in (load, authenticate, read)):
            raise ValueError("explicit_audit_authorities_required")
        self.api, self.load, self.authenticate, self.read_run = api, load, authenticate, read

    @staticmethod
    def _registration(registered, plan):
        reg = registered.registration if isinstance(registered, RegisteredAuditRun) else None
        if (
            type(reg) is not dict
            or registered.plan_id != plan["plan_id"]
            or registered.plan_digest != plan["plan_digest"]
            or reg.get("plan_id") != plan["plan_id"]
            or not ID.fullmatch(str(reg.get("run_id")))
            or type(reg.get("record_count")) is not int
            or not 1 <= reg["record_count"] <= MAX_RECORDS
            or not HASH.fullmatch(str(reg.get("chain_digest")))
            or type(registered.anchors) is not tuple
        ):
            raise ValueError("invalid_registered_audit")
        return reg

    @staticmethod
    def _project(registered, reg, run, plan):
        records = run.records
        if (
            run.registered is not True
            or (run.plan_id, run.run_id) != (plan["plan_id"], reg["run_id"])
            or run.record_count != reg["record_count"]
            or len(records) != reg["record_count"]
            or run.chain_digest != reg["chain_digest"]
            or records[-1]["sha256"] != reg["chain_digest"]
        ):
            raise ValueError("registration_mismatch")
        # An earlier registration must still be a prefix: the run grew, it was not rewritten.
        for count, digest in registered.anchors:
            if type(count) is not int or not 1 <= count <= len(records) or records[count - 1]["sha256"] != digest:
                raise ValueError("registration_rewritten")
        origins, rows, tool_records = set(), [], 0
        for record in records:
            if record["origin_id"] in origins:
                raise ValueError("duplicate_audit_origin")
            origins.add(record["origin_id"])
            if record["source"] != "agent.tools":
                continue
            tool_records += 1
            payload = record["payload"]
            if record["kind"] != "DESTRUCTIVE_ATTEMPT_BLOCKED" or payload.get("category") != "bucket_delete":
                continue
            attempt = payload.get("attempt")
            if (
                payload.get("outcome") != "DENIED"
                or payload.get("reason") != record["kind"]
                or type(attempt) is not int
                or not 1 <= attempt <= 1000000
                or not re.fullmatch(
                    rf"t11:{re.escape(reg['run_id'])}:attempt{attempt}:[0-9]+",
                    record["origin_id"],
                )
            ):
                raise ValueError("inconsistent_tool_denial")
            tool = payload.get("tool")
            # Deliberately no raw payload, command, arbitrary message, URL or key.
            rows.append(
                {
                    "record_id": record["record_id"],
                    "recorded_at": record["recorded_at"],
                    "origin_id": record["origin_id"],
                    "attempt": attempt,
                    "tool": tool if isinstance(tool, str) and TOOL.fullmatch(tool) else None,
                    "outcome": "DENIED",
                    "reason": "DESTRUCTIVE_ATTEMPT_BLOCKED",
                    "category": "bucket_delete",
                }
            )
        return {
            "kind": "console-agent-audit.v1",
            "plan_id": plan["plan_id"],
            "plan_digest": plan["plan_digest"],
            "run_id": reg["run_id"],
            "record_count": reg["record_count"],
            "chain_digest": reg["chain_digest"],
            # 0 means no T11 tool record reached this run, not "no attempt was made".
            "tool_records": tool_records,
            "rows_total": len(rows),
            "unregistered_records": run.unregistered_records,
            "authorizes_cutover": False,
            "retention_verified": False,
            "rows": rows[-MAX_ROWS:],
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
            reg = self._registration(registered, before)
            run = await asyncio.wait_for(
                asyncio.to_thread(self.read_run, before["plan_id"], reg["run_id"], dict(reg)), 10
            )
            result = self._project(registered, reg, run, before)
            after = await _plan(self.api, scope, plan_id)
            # The plan moves on while it runs; only its identity, digest and the reader's
            # authority must hold.
            if (
                (after["plan_id"], after["plan_digest"]) != (before["plan_id"], before["plan_digest"])
                or await asyncio.wait_for(self.authenticate(scope, dict(after), registered), 3)
                is not True
            ):
                raise ObservationError("audit_authority_changed")
            return result
        except ObservationError:
            raise
        except Exception:
            raise ObservationError("audit_unavailable") from None


def t24_reader(api, *, load, authenticate, client=None, bucket=None, root=None, prefix="audit"):
    """Composition hook over T24's public reader; requires the installed T24 package.

    S3 (`client`, `bucket`) checks versions and delete markers. A local FileSink `root` cannot
    detect overwrites and is for tests and demos only. Missing package is a startup error, never
    a local replacement verifier.
    """
    from bravo_audit.reader import read_local_run, read_run
    from bravo_audit.registration import check_registration

    if (client is None or not bucket) == (root is None):
        raise ValueError("one_audit_store_required")

    def read(plan_id, run_id, registration):
        registration = check_registration(registration)
        if root is not None:
            return read_local_run(root, plan_id, run_id, prefix=prefix, registration=registration)
        return read_run(client, bucket, prefix, plan_id, run_id, registration=registration)

    return PlanAudit(api, load=load, authenticate=authenticate, read=read)
