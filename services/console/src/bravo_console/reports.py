"""Read-only projection of registered T19 security reports, never an evaluator.

The host must authenticate immutable report origin and tenant/plan ownership.
No report upload, client-selected profile, remote URL, or implicit trust callback.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from .observations import ObservationError, _plan

CONTRACTS = Path(__file__).parent / "contracts"
SCHEMA = json.loads((CONTRACTS / "control-result.schema.json").read_text())
CATALOG = json.loads((CONTRACTS / "profiles.json").read_text())
VALIDATOR = Draft202012Validator(
    {"$ref": "#/$defs/GateComparison", "$defs": SCHEMA["$defs"]},
    format_checker=FormatChecker(),
)


@dataclass(frozen=True, repr=False)
class RegisteredGateDocument:
    plan_id: str
    plan_digest: str
    document: bytes


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate report field")
        value[key] = item
    return value


def _constant(_):
    raise ValueError("nonfinite report number")


class PlanControlReport:
    def __init__(self, api, *, load, authenticate, clock=None):
        if not callable(load) or not callable(authenticate):
            raise ValueError("explicit_registered_report_authority_required")
        self.api, self.load, self.authenticate = api, load, authenticate
        self.clock = clock or (lambda: datetime.now(UTC))

    def _project(self, registered, plan):
        if (
            not isinstance(registered, RegisteredGateDocument)
            or registered.plan_id != plan["plan_id"]
            or registered.plan_digest != plan["plan_digest"]
            or type(registered.document) is not bytes
            or not 1 <= len(registered.document) <= 262144
        ):
            raise ObservationError("control_report_scope_mismatch")
        report = json.loads(
            registered.document, object_pairs_hook=_unique, parse_constant=_constant
        )
        VALIDATOR.validate(report)
        if report["app"] != plan["workload_id"] or any(
            report[side + "_environment_id"] != plan[side + "_environment_id"]
            for side in ("source", "target")
        ):
            raise ObservationError("control_report_scope_mismatch")
        profiles = [
            p
            for p in CATALOG["profiles"]
            if (p["id"], p["version"]) == (report["profile"]["id"], report["profile"]["version"])
        ]
        if len(profiles) != 1 or profiles[0]["app"] != report["app"]:
            raise ObservationError("unknown_control_profile")
        expected = profiles[0]["required_test_ids"]
        rows = report["rows"]
        if len(rows) != len(expected) or sorted(r["test_id"] for r in rows) != sorted(expected):
            raise ObservationError("incomplete_control_report")
        if any(CATALOG["tests"][r["test_id"]]["control_id"] != r["control_id"] for r in rows):
            raise ObservationError("invalid_control_identity")
        # Defensive read-model consistency, not a substitute for T19 re-evaluation.
        if report["decision"] == "PASS" and (
            report["run_issues"] or any(r["decision"] == "BLOCK" for r in rows)
        ):
            raise ObservationError("inconsistent_control_report")
        evaluated = datetime.fromisoformat(report["evaluated_at"].replace("Z", "+00:00"))
        now = self.clock()
        if evaluated.utcoffset() is None or now.utcoffset() is None or evaluated > now:
            raise ObservationError("invalid_control_report_time")
        return {
            "kind": "console-control-report.v1",
            "plan_id": plan["plan_id"],
            "plan_digest": plan["plan_digest"],
            "source_environment_id": plan["source_environment_id"],
            "target_environment_id": plan["target_environment_id"],
            "gate_id": report["gate_id"],
            "context_digest": report["context_digest"],
            "report_digest": hashlib.sha256(registered.document).hexdigest(),
            "profile": report["profile"],
            "decision": report["decision"],
            "evaluated_at": evaluated.isoformat(),
            "queried_at": now.isoformat(),
            "authorizes_cutover": False,
            "run_issues": report["run_issues"],
            # No arbitrary question/excerpt/URL, raw evidence or approver identity.
            "rows": [
                {
                    k: row[k]
                    for k in (
                        "test_id",
                        "control_id",
                        "source_verdict",
                        "target_verdict",
                        "decision",
                        "reason",
                        "improved",
                    )
                }
                | {"exception_applied": row["exception"] is not None}
                for row in rows
            ],
        }

    async def read(self, scope, plan_id):
        try:
            before = await _plan(self.api, scope, plan_id)
            registered = await asyncio.wait_for(self.load(scope, dict(before)), timeout=3)
            if (
                await asyncio.wait_for(
                    self.authenticate(scope, dict(before), registered), timeout=3
                )
                is not True
            ):
                raise ObservationError("control_report_unverified")
            result = self._project(registered, before)
            after = await _plan(self.api, scope, plan_id)
            if after != before:
                raise ObservationError("control_report_plan_changed")
            if (
                await asyncio.wait_for(self.authenticate(scope, dict(after), registered), timeout=3)
                is not True
            ):
                raise ObservationError("control_report_unverified")
            return result
        except ObservationError:
            raise
        except Exception:
            # Never serialize raw validation, storage, signature or credential errors.
            raise ObservationError("control_report_unavailable") from None
