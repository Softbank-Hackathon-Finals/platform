"""Trusted in-process collectors → complete, deterministic T5 ControlRuns.

This is not an ingestion endpoint or an authorization boundary. The orchestrator
owns the catalog, environment registry, collectors and evidence storage. Uploaded
JSON, tool reports and AI assertions must never be registered as trusted probes.
"""

import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

OUTCOMES = frozenset(
    {"deny", "allow", "present", "absent", "enabled", "disabled", "success", "failure"}
)
REASONS = frozenset(
    {
        "POSITIVE_CONTROL_FAILED",
        "OBSERVATION_DENIED",
        "UNEXPECTED_RESPONSE",
        "TIMEOUT",
        "TOOL_ERROR",
        "NOT_RUN",
    }
)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}\Z")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_TTL = timedelta(hours=24)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid registered identifier")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Environment:
    id: str
    role: str
    ref: str

    def __post_init__(self):
        _identifier(self.id)
        _identifier(self.ref)
        if self.role not in {"source", "target", "rehearsal"}:
            raise ValueError("invalid environment role")


@dataclass(frozen=True)
class TestSpec:
    test_id: str
    control_id: str
    method_version: str
    expected: str
    positive_control: str


@dataclass(frozen=True)
class RunContext:
    run_id: str
    app: str
    environment: Environment


@dataclass(frozen=True)
class Observation:
    """Collector assertion bound to this invocation, with no secret-bearing text.

    output_sha256 identifies retained collector evidence, not a signature or proof
    that the observation is true. A trusted collector must implement the complete
    T5 method (including matching resources and positive controls).
    """

    context: RunContext
    test_id: str
    method_version: str
    observed: str | None
    positive_control: str
    collected_at: datetime
    tool: str
    tool_version: str
    output_sha256: str
    unknown_reason: str | None = None


class Catalog:
    """Validated snapshot of a server-selected T5 catalog and schema."""

    def __init__(self, document: dict, schema: dict):
        self._schema = deepcopy(schema)
        Draft202012Validator.check_schema(self._schema)
        self._validator("EvaluationProfiles").validate(document)
        self._tests = MappingProxyType(
            {
                test_id: TestSpec(test_id=test_id, **deepcopy(values))
                for test_id, values in document["tests"].items()
            }
        )
        profiles = {}
        for test_id, spec in self._tests.items():
            if test_id.rsplit(".", 1)[0] != spec.control_id:
                raise ValueError("catalog test/control mismatch")
            _identifier(spec.method_version)
        for profile in document["profiles"]:
            key = (profile["id"], profile["version"], profile["app"])
            if key in profiles:
                raise ValueError("duplicate evaluation profile")
            for value in key:
                _identifier(value)
            ids = tuple(profile["required_test_ids"])
            if not ids or len(ids) != len(set(ids)) or not set(ids) <= self._tests.keys():
                raise ValueError("invalid required test set")
            profiles[key] = ids
        self._profiles = MappingProxyType(profiles)

    def _validator(self, definition: str) -> Draft202012Validator:
        schema = {"$ref": f"#/$defs/{definition}", "$defs": self._schema["$defs"]}
        return Draft202012Validator(schema, format_checker=FormatChecker())

    def profile(self, profile_id: str, version: str, app: str) -> tuple[TestSpec, ...]:
        try:
            ids = self._profiles[(profile_id, version, app)]
        except KeyError:
            raise ValueError("unregistered evaluation profile or app") from None
        return tuple(self._tests[test_id] for test_id in ids)

    def validate_run(self, run: dict) -> None:
        self._validator("ControlRun").validate(run)


Probe = Callable[[RunContext, TestSpec], Observation]


class Runner:
    def __init__(
        self,
        catalog: Catalog,
        probes: Mapping[str, Probe],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        evidence_ttl: timedelta = MAX_TTL,
    ):
        if not timedelta(0) < evidence_ttl <= MAX_TTL:
            raise ValueError("evidence TTL must be within 24 hours")
        if any(
            test_id not in catalog._tests or not callable(probe)
            for test_id, probe in probes.items()
        ):
            raise ValueError("invalid collector registration")
        self._catalog = catalog
        self._probes = dict(probes)
        self._clock = clock
        self._ttl = evidence_ttl

    def run(
        self, *, profile_id: str, profile_version: str, app: str, environment: Environment
    ) -> dict:
        specs = self._catalog.profile(profile_id, profile_version, app)
        context = RunContext(str(uuid4()), app, environment)
        started = _utc(self._clock())
        rows = []
        previous = started
        for spec in specs:
            before = _utc(self._clock())
            if before < previous:
                raise ValueError("collector clock moved backwards")
            probe = self._probes.get(spec.test_id)
            observation = None
            reason = "NOT_RUN"
            if probe is not None:
                try:
                    observation = probe(context, spec)
                    reason = None
                except TimeoutError:
                    reason = "TIMEOUT"
                except PermissionError:
                    reason = "OBSERVATION_DENIED"
                except Exception:
                    # Exception messages can contain credentials, URLs or raw tool output.
                    reason = "TOOL_ERROR"
            after = _utc(self._clock())
            if after < before:
                raise ValueError("collector clock moved backwards")
            row = self._row(context, spec, observation, reason, before, after)
            rows.append(row)
            previous = after
        finished = _utc(self._clock())
        if finished < previous:
            raise ValueError("collector clock moved backwards")
        run = {
            "kind": "control_run",
            "schema_version": "1",
            "run_id": context.run_id,
            "app": app,
            "environment": {"id": environment.id, "role": environment.role, "ref": environment.ref},
            "started_at": _timestamp(started),
            "finished_at": _timestamp(finished),
            "results": rows,
        }
        self._catalog.validate_run(run)
        return run

    def _row(self, context, spec, observation, reason, before, after):
        row = {
            "test_id": spec.test_id,
            "control_id": spec.control_id,
            "method_version": spec.method_version,
            "expected": spec.expected,
            "observed": None,
            "positive_control": "fail" if spec.positive_control == "required" else "not_applicable",
            "verdict": "UNKNOWN",
            "unknown_reason": reason,
            "evidence": None,
            "started_at": _timestamp(before),
            "duration_ms": int((after - before).total_seconds() * 1000),
        }
        if reason is not None:
            return row
        try:
            self._check_observation(context, spec, observation, before, after)
        except (ValueError, TypeError, AttributeError):
            row["unknown_reason"] = "TOOL_ERROR"
            return row
        row["observed"] = observation.observed
        row["positive_control"] = observation.positive_control
        row["evidence"] = {
            "collected_at": _timestamp(observation.collected_at),
            "expires_at": _timestamp(_utc(observation.collected_at) + self._ttl),
            "tool": observation.tool,
            "tool_version": observation.tool_version,
            "output_sha256": observation.output_sha256,
        }
        if spec.positive_control == "required" and observation.positive_control != "pass":
            row["unknown_reason"] = "POSITIVE_CONTROL_FAILED"
        elif observation.unknown_reason is not None:
            row["unknown_reason"] = observation.unknown_reason
        elif observation.observed is None:
            row["unknown_reason"] = "UNEXPECTED_RESPONSE"
        else:
            row["verdict"] = "PASS" if observation.observed == spec.expected else "FAIL"
            row["unknown_reason"] = None
        return row

    @staticmethod
    def _check_observation(context, spec, observation, before, after):
        if not isinstance(observation, Observation):
            raise ValueError("invalid collector result")
        if observation.context != context or observation.test_id != spec.test_id:
            raise ValueError("collector scope mismatch")
        if observation.method_version != spec.method_version:
            raise ValueError("collector method mismatch")
        if observation.observed is not None and observation.observed not in OUTCOMES:
            raise ValueError("invalid observed outcome")
        if observation.unknown_reason is not None and observation.unknown_reason not in REASONS:
            raise ValueError("invalid unknown reason")
        if observation.positive_control not in {"pass", "fail", "not_applicable"}:
            raise ValueError("invalid positive control")
        if (
            spec.positive_control == "not_applicable"
            and observation.positive_control != "not_applicable"
        ):
            raise ValueError("unexpected positive control")
        if not before <= _utc(observation.collected_at) <= after:
            raise ValueError("collector evidence outside invocation window")
        _identifier(observation.tool)
        _identifier(observation.tool_version)
        if not isinstance(observation.output_sha256, str) or not SHA256.fullmatch(
            observation.output_sha256
        ):
            raise ValueError("invalid evidence digest")
