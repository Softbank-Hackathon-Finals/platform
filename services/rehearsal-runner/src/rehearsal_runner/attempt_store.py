"""Write-once attempt records owned by the trusted runner host.

T10 admits each attempt; the runner claims it once and never re-executes a
claim. A claim without a result is an unknown outcome for the operator, not a
reason to resubmit. Records are HMAC-signed with a key only the runner host
holds, so model output or a copied file cannot become evidence. T10 reads the
PASS/FAIL observation and the PASS original from these records.
"""

import hashlib
import hmac
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .admission import run_admitted_attempt
from .approval_handoff import CandidateSubmission
from .retry import AttemptFactory, AttemptPorts
from .runner import AttemptResult, Namespace

_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class RecordUnavailable(RuntimeError):
    """A record is missing its signature, was altered, or names another attempt."""


class AttemptStore:
    """Directory of signed records; only the runner host may hold `key`."""

    def __init__(self, root: Path, key: bytes):
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("record key must be at least 32 bytes")
        self.root, self._key = Path(root), key
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, rehearsal_id: str, attempt: int) -> Path:
        if (not isinstance(rehearsal_id, str) or _RUN.fullmatch(rehearsal_id) is None
                or type(attempt) is not int or not 1 <= attempt <= 3):
            raise ValueError("invalid attempt identity")
        return self.root / rehearsal_id / str(attempt)

    def _signed(self, value: dict) -> bytes:
        mac = hmac.new(self._key, _canonical(value), hashlib.sha256).hexdigest()
        return _canonical({"record": value, "mac": mac})

    def _create(self, path: Path, data: bytes) -> bool:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return True

    def _load(self, path: Path) -> dict | None:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(1048577)
        try:
            outer = json.loads(raw)
            value, mac = outer["record"], outer["mac"]
            expected = hmac.new(self._key, _canonical(value), hashlib.sha256).hexdigest()
        except (ValueError, KeyError, TypeError):
            raise RecordUnavailable("unreadable record") from None
        if set(outer) != {"record", "mac"} or not hmac.compare_digest(mac, expected):
            raise RecordUnavailable("record signature mismatch")
        return value

    def claim(self, admission: object) -> bool:
        """Return True once per attempt; later calls never re-run it."""
        directory = self._path(admission.rehearsal_id, admission.attempt)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self._create(directory / "claim.json", self._signed({"admission": asdict(admission)}))

    def write(self, admission: object, body: dict) -> dict:
        directory = self._path(admission.rehearsal_id, admission.attempt)
        claim = self._load(directory / "claim.json")
        if claim is None or claim["admission"] != asdict(admission):
            raise RecordUnavailable("attempt was not claimed")
        record = dict(body, admission=asdict(admission),
                      evidence_ref=f"{admission.rehearsal_id}-{admission.attempt}")
        record["evidence_digest"] = hashlib.sha256(_canonical(record)).hexdigest()
        record["verification_id"] = "verification-" + record["evidence_digest"][:40]
        if not self._create(directory / "result.json", self._signed(record)):
            raise RecordUnavailable("attempt already recorded")
        return record

    def read(self, rehearsal_id: str, attempt: int) -> dict | None:
        directory = self._path(rehearsal_id, attempt)
        record = self._load(directory / "result.json")
        if record is not None and (record["admission"]["rehearsal_id"] != rehearsal_id
                                   or record["admission"]["attempt"] != attempt):
            raise RecordUnavailable("record names another attempt")
        return record

    def claimed(self, rehearsal_id: str, attempt: int) -> bool:
        return self._load(self._path(rehearsal_id, attempt) / "claim.json") is not None


@dataclass(frozen=True)
class PlanBinding:
    """Registered plan values the host reads from T10's trusted plan record."""

    plan_digest: str
    target_environment_id: str


class AttemptPlanner(Protocol):
    def binding(self, admission: object) -> PlanBinding: ...

    def factory(self, admission: object, task: object) -> AttemptFactory:
        """Build real ports for this attempt; must not execute remote effects."""
        ...


@dataclass
class _Trace:
    created: Namespace | None = None
    removed_uid: str | None = None
    probe_run_id: str | None = None


class _TracedLifecycle:
    def __init__(self, inner, trace: _Trace, attempt: int):
        self.inner, self.trace = inner, trace
        self.attempt = getattr(inner, "attempt", attempt)

    def create(self, run_id: str) -> Namespace:
        self.trace.created = self.inner.create(run_id)
        return self.trace.created

    def cleanup(self, namespace: Namespace) -> bool:
        cleaned = self.inner.cleanup(namespace) is True
        if cleaned:
            self.trace.removed_uid = namespace.uid
        return cleaned


class _TracedVerifier:
    def __init__(self, inner, trace: _Trace):
        self.inner, self.trace = inner, trace
        self.attempt = inner.attempt

    def verify_isolated(self, proposal, namespace, isolation):
        self.trace.probe_run_id = isolation.probe_run_id
        if hasattr(self.inner, "verify_isolated"):
            return self.inner.verify_isolated(proposal, namespace, isolation)
        return self.inner.verify(proposal, namespace)


@dataclass(frozen=True)
class _TracedFactory:
    inner: AttemptFactory
    trace: _Trace

    def prepare(self, attempt: int, feedback_ref: str | None) -> AttemptPorts:
        ports = self.inner.prepare(attempt, feedback_ref)
        return AttemptPorts(ports.proposer, _TracedLifecycle(ports.lifecycle, self.trace, attempt),
                            ports.isolation, _TracedVerifier(ports.verifier, self.trace))


def judge_attempt(result: AttemptResult, trace: _Trace) -> dict:
    """Map one attempt to PASS/FAIL only when every identity is bound."""
    evidence = result.untrusted_receipt
    if result.state != "RECEIPT_UNVERIFIED" or not hasattr(evidence, "public"):
        return {"verdict": "NEEDS_HUMAN", "reason": result.reason}
    created = trace.created
    if (created is None or trace.removed_uid != created.uid or trace.probe_run_id is None
            or result.proposal is None or evidence.namespace != created.name
            or evidence.namespace_uid != created.uid
            or evidence.proposal_digest != result.proposal.proposal_digest
            or evidence.verdict not in ("PASS", "FAIL")):
        return {"verdict": "NEEDS_HUMAN", "reason": "EVIDENCE_IDENTITY_MISMATCH"}
    return {"verdict": evidence.verdict, "reason": evidence.reason, "namespace": created.name,
            "created_uid": created.uid, "removed_uid": trace.removed_uid, "absence_confirmed": True,
            "isolation_probe_run_id": trace.probe_run_id, "proposal_digest": evidence.proposal_digest,
            "diff_hash": evidence.diff_hash, "image_digest": evidence.image_digest,
            "diff": evidence.diff.decode("utf-8"), "evidence": evidence.public()}


@dataclass(frozen=True)
class RecordedAttemptSender:
    """T10 `AttemptSender`: claim once, run the admitted attempt, sign its record.

    `dispatch` lets the host run the attempt outside T10's request; it must
    not retry. A crash after claiming leaves the attempt unconfirmed. T10
    confirms a PASS only after its intake received the original, so `deliver`
    sends the PASS candidate once; an unknown delivery outcome leaves the
    signed record unchanged and the operator may redeliver it idempotently.
    """

    store: AttemptStore = field(repr=False)
    planner: AttemptPlanner = field(repr=False)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    validity: timedelta = timedelta(hours=1)
    dispatch: Callable[[Callable[[], None]], None] = field(default=lambda work: work(), repr=False)
    deliver: Callable[[CandidateSubmission], object] | None = field(default=None, repr=False)

    def submit_once(self, admission: object, task: object) -> None:
        if not self.store.claim(admission):
            return
        self.dispatch(lambda: self._execute(admission, task))

    def _feedback(self, admission) -> str | None:
        if admission.attempt == 1:
            return None
        previous = self.store.read(admission.rehearsal_id, admission.attempt - 1)
        if previous is None or previous["verdict"] != "FAIL":
            raise RecordUnavailable("previous attempt did not fail")
        return previous["evidence_digest"]

    def _execute(self, admission, task) -> None:
        body = {"verdict": "NEEDS_HUMAN", "reason": "EXECUTION_UNAVAILABLE"}
        try:
            binding = self.planner.binding(admission)
            trace = _Trace()
            factory = _TracedFactory(self.planner.factory(admission, task), trace)
            result = run_admitted_attempt(admission, task, factory, feedback_digest=self._feedback(admission))
            body = judge_attempt(result, trace) | {"plan_digest": binding.plan_digest,
                                                   "target_environment_id": binding.target_environment_id}
        except Exception:
            pass
        now = self.clock()
        body |= {"recorded_at": now.isoformat(), "expires_at": (now + self.validity).isoformat()}
        record = self.store.write(admission, body)
        if record["verdict"] == "PASS" and self.deliver is not None:
            try:
                self.deliver(RecordedEvidence(self.store).submission(
                    admission.plan_id, admission.rehearsal_id, record["verification_id"]))
            except Exception:
                pass


@dataclass(frozen=True)
class RecordedEvidence:
    """T10 `EvidenceReader` and `ChangeIntake.read_original` over signed records."""

    store: AttemptStore = field(repr=False)

    def read_authenticated(self, admission: object):
        from bravo_platform_events.rehearsal import Observation

        record = self.store.read(admission.rehearsal_id, admission.attempt)
        if record is None or record["verdict"] not in ("PASS", "FAIL"):
            return None  # Running, or needs a human: T10 keeps it UNCONFIRMED.
        if record["admission"] != asdict(admission):
            raise RecordUnavailable("record names another admission")
        return Observation(admission, record["verification_id"], record["proposal_digest"],
                           record["namespace"], record["created_uid"], record["removed_uid"],
                           record["absence_confirmed"] is True, record["verdict"])

    def _passed(self, plan_id: str, run_id: str, verification_id: str) -> dict:
        for attempt in (1, 2, 3):
            record = self.store.read(run_id, attempt)
            if record is not None and record["verification_id"] == verification_id:
                if record["verdict"] != "PASS" or record["admission"]["plan_id"] != plan_id:
                    break
                return record
        raise RecordUnavailable("no passing record for this verification")

    def submission(self, plan_id: str, run_id: str, verification_id: str) -> CandidateSubmission:
        """Candidate fields the T15 workload sends; T10 re-reads the original."""
        record = self._passed(plan_id, run_id, verification_id)
        return CandidateSubmission(
            plan_id=plan_id, run_id=run_id, attempt=record["admission"]["attempt"],
            verification_id=verification_id, evidence_ref=record["evidence_ref"],
            proposal_digest=record["proposal_digest"], diff_hash=record["diff_hash"],
            image_digest=record["image_digest"], target_environment_id=record["target_environment_id"],
            namespace=record["namespace"], namespace_uid=record["created_uid"],
            cleanup_uid=record["removed_uid"])

    def read_original(self, plan_id: str, run_id: str, verification_id: str, evidence_ref: str):
        from bravo_platform_events.change_intake import Candidate, SourceVerification

        record = self._passed(plan_id, run_id, verification_id)
        if record["evidence_ref"] != evidence_ref:
            raise RecordUnavailable("evidence reference mismatch")
        admission = record["admission"]
        candidate = Candidate(**self.submission(plan_id, run_id, verification_id).t10_fields())
        return SourceVerification(candidate, admission["tenant_id"], record["plan_digest"],
                                  admission["plan_version"], record["expires_at"], "PASS",
                                  record["created_uid"], record["removed_uid"],
                                  record["absence_confirmed"] is True)
