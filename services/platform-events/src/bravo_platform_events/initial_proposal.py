"""T10's initial PROPOSE boundary, using the existing plan lock and journal.

These host ports do not install an HTTP endpoint, model adapter or publisher.
T11 owns model input conversion; T38 authenticates the actual execution result;
T7's reader authenticates publication. None can be supplied by an HTTP caller.
"""

import hashlib
import json
from dataclasses import asdict
from typing import Annotated, Literal
from uuid import uuid4

from bravo_agent.contracts import Contract, Digest, Proposal, SnapshotManifest, digest, safe_text
from bravo_agent.product_execution_contracts import ExecutionPending, ExecutionUnknown
from pydantic import Field, field_validator
from rehearsal_runner.admission import logical_task_digest

from .cutover import _get, _put
from .executor import _timestamp
from .store import EventError
from .workflow import Change, Evidence
from .workflow import Proposal as WorkflowProposal

PURPOSE = "t38-initial-proposal"
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
SourceSha = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]


class InitialBinding(Contract):
    plan_id: Identifier
    tenant_id: Identifier
    plan_digest: Digest
    target_environment_id: Identifier
    plan_version: int = Field(ge=1)
    task_id: Identifier
    task_digest: Digest
    source_sha: SourceSha
    collection_scope_digest: Digest
    inventory_digest: Digest
    comparison_digest: Digest
    analysis_receipt_id: Identifier
    stage: Literal["PROPOSE"] = "PROPOSE"
    purpose: Literal["t38-initial-proposal"] = PURPOSE


class InitialAnalysis(Contract):
    inventory_digest: Digest
    item_id: str = Field(pattern=r"^item-[0-9]{3}$")
    grade: Literal["code_change"]
    rationale: str = Field(min_length=1, max_length=4096)

    @field_validator("rationale")
    @classmethod
    def checked_rationale(cls, value):
        if not value.strip():
            raise ValueError("empty initial analysis")
        return safe_text(value)


class InitialSource(Contract):
    schema_version: Literal[2]
    analysis: InitialAnalysis
    manifest: SnapshotManifest
    files: dict[str, Annotated[str, Field(max_length=32768)]] = Field(min_length=1, max_length=64, repr=False)


class InitialInput(InitialSource):
    binding: InitialBinding
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class InitialFields(Contract):
    request_id: str = Field(pattern=r"^t10p-[a-f0-9]{64}$")
    plan_id: Identifier
    plan_version: int = Field(ge=1)
    input_ref: Identifier
    input_digest: Digest
    binding_digest: Digest
    runtime_source_sha: SourceSha
    purpose: Literal["t38-initial-proposal"] = PURPOSE


class InitialIntent(InitialFields):
    recipient_public_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class InitialResult(Contract):
    """Returned only by T38's authenticated run/artifact reader, not the model."""

    request_id: str
    input_digest: Digest
    runtime_source_sha: SourceSha
    execution_receipt_id: Identifier
    proposal: Proposal
    proposal_digest: Digest
    schema_valid: bool = Field(strict=True)
    lint_passed: bool = Field(strict=True)


class CandidatePublication(Contract):
    """Normalized by the installed T7 reader after actual publication checks.

    The reader verifies repository/workflow/run/attempt/artifact provenance,
    scan success, exact proposal-to-diff mapping, and registry readback. The
    strings here bind that verified record; they do not establish its authority.
    source_sha is the original registered source (T7 receipt.base_sha). T7's
    receipt.source_sha is the edited candidate head and must be checked by the
    reader against that exact proposal; it is not the original source field.
    """

    receipt_id: Identifier
    request_id: str
    plan_digest: Digest
    source_sha: SourceSha
    proposal_digest: Digest
    diff_hash: ImageDigest
    image_digest: ImageDigest


def _validated(model, value):
    raw = value.model_dump(mode="json") if isinstance(value, Contract) else value
    return model.model_validate_json(json.dumps(raw, allow_nan=False))


def _source(value):
    value = _validated(InitialSource, value)
    if set(value.files) != set(value.manifest.files):
        raise ValueError("source files mismatch")
    for path, content in value.files.items():
        data = safe_text(content).encode("utf-8")
        if len(data) > 32768 or hashlib.sha256(data).hexdigest() != value.manifest.files[path]:
            raise ValueError("source content mismatch")
    return value


def _slot(version):
    return f"initial-proposal:{version}"


class InitialProposalRecords:
    """One initial input/result per PROPOSE version. No state setter or executor.

    catalog.for_plan and source(c, plan, task) are current trusted readers.
    authorize uses ProductInputAccess with the *separate* initial purpose grant.
    The source returns a registered immutable snapshot, never caller files.
    """

    def __init__(self, store, *, catalog, source, authorize, clock):
        if not callable(getattr(catalog, "for_plan", None)) or not all(map(callable, (source, authorize, clock))):
            raise ValueError("initial_proposal_ports_required")
        self.store, self.catalog, self.source = store, catalog, source
        self.authorize, self.clock = authorize, clock

    def _current(self, c, *, plan_id, tenant_id, task_id, version, principal, promoted=False):
        row = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None or row["tenant_id"] != tenant_id:
            raise EventError("initial_proposal_unavailable")
        plan = dict(row)
        state = (plan["state"], plan["version"])
        if state != ("PROPOSE", version) and not (promoted and state == ("REHEARSE", version + 1)):
            raise EventError("initial_proposal_unavailable")
        previous = _get(c, plan_id, f"workflow:{version - 1}")
        try:
            spec = json.loads(plan["spec_json"])
            proof = previous[1]
            analysis = proof["result"]
            if (proof["plan_id"], proof["plan_digest"], proof["target_environment_id"], proof["version"]) != (
                    plan_id, spec["plan_digest"], spec["target_environment_id"], version - 1):
                raise ValueError()
            if set(analysis) != {"scope_digest", "inventory_digest", "comparison_digest"}:
                raise ValueError()
            if analysis["scope_digest"] != spec["collection_scope_digest"]:
                raise ValueError()
            task = self.catalog.for_plan(c, plan, task_id)
            binding = InitialBinding(plan_id=plan_id, tenant_id=tenant_id,
                plan_digest=spec["plan_digest"], target_environment_id=spec["target_environment_id"],
                plan_version=version, task_id=task_id, task_digest=logical_task_digest(task),
                source_sha=task.source_sha, collection_scope_digest=analysis["scope_digest"],
                inventory_digest=analysis["inventory_digest"], comparison_digest=analysis["comparison_digest"],
                analysis_receipt_id=proof["receipt_id"])
            self.authorize(c, principal, plan, binding, PURPOSE)
            source = _source(self.source(c, plan, task))
            if (source.manifest.source_sha != binding.source_sha
                    or source.analysis.inventory_digest != binding.inventory_digest):
                raise ValueError()
            self.authorize(c, principal, plan, binding, PURPOSE)
            value = InitialInput(**source.model_dump(), binding=binding,
                run_id="proposal-" + digest(binding)[:48])
        except Exception:
            raise EventError("initial_proposal_unavailable") from None
        return plan, value

    def admit(self, principal, *, plan_id, tenant_id, task_id, expected_version, expires_at):
        if type(expected_version) is not int or expected_version < 1:
            raise EventError("initial_proposal_unavailable")
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            _plan, value = self._current(c, plan_id=plan_id, tenant_id=tenant_id, task_id=task_id,
                                        version=expected_version, principal=principal)
            if _timestamp(expires_at) <= _timestamp(self.clock()):
                raise EventError("initial_proposal_expired")
            request = dict(principal=principal, expires_at=expires_at, input=value.model_dump(mode="json"))
            slot = _slot(expected_version)
            old = _get(c, plan_id, slot)
            if old:
                if old[0] != request:
                    raise EventError("initial_proposal_conflict")
                return old[1]
            ref = dict(input_ref="initial-" + uuid4().hex, input_digest=digest(value))
            _put(c, plan_id, slot, request, ref)
            return ref

    def _read(self, c, principal, plan_id, version, ref, *, promoted=False):
        old = _get(c, plan_id, _slot(version))
        if not old or old[1] != ref or old[0]["principal"] != principal:
            raise EventError("initial_proposal_unavailable")
        saved = old[0]
        binding = saved["input"]["binding"]
        if _timestamp(saved["expires_at"]) <= _timestamp(self.clock()):
            raise EventError("initial_proposal_expired")
        plan, value = self._current(c, plan_id=plan_id, tenant_id=binding["tenant_id"],
            task_id=binding["task_id"], version=version, principal=principal, promoted=promoted)
        if value.model_dump(mode="json") != saved["input"] or digest(value) != ref["input_digest"]:
            raise EventError("initial_proposal_conflict")
        if plan["state"] == "REHEARSE":
            publication = _get(c, plan_id, _slot(version) + ":publication")
            checkpoint = _get(c, plan_id, f"workflow:{version}")
            if not publication or not checkpoint or checkpoint[1] != asdict(self._proof(value, publication[1])):
                raise EventError("initial_proposal_unavailable")
        return value

    def read_input(self, principal, plan_id, version, **ref):
        with self.store._transaction() as c:
            return self._read(c, principal, plan_id, version, ref).model_dump(mode="json")

    @staticmethod
    def _proof(value, publication):
        binding = value.binding
        publication = _validated(CandidatePublication, publication)
        return Evidence(binding.plan_id, binding.plan_digest, binding.target_environment_id,
            binding.plan_version, publication.receipt_id,
            WorkflowProposal(binding.inventory_digest, binding.comparison_digest,
                Change(publication.diff_hash, publication.image_digest), True, True))


class InitialProposalExecution:
    """T38 host adapter: durable claim before one submit, then read only.

    client.recipient/submit/read/validate are installed trusted ports. validate
    authenticates the remote run/artifact and returns InitialResult. A dictionary
    containing success booleans is never accepted directly as provider evidence.
    """

    def __init__(self, records, client, *, principal, runtime_source_sha):
        if type(records) is not InitialProposalRecords or any(
                not callable(getattr(client, method, None)) for method in ("recipient", "submit", "read", "validate")):
            raise ValueError("initial_execution_ports_required")
        self.records, self.client = records, client
        self.principal, self.runtime_source_sha = principal, runtime_source_sha

    def _fields(self, value, ref):
        fields = dict(plan_id=value.binding.plan_id, plan_version=value.binding.plan_version,
                      **ref, binding_digest=digest(value.binding),
                      runtime_source_sha=self.runtime_source_sha, purpose=PURPOSE)
        return InitialFields(**fields,
            request_id="t10p-" + digest(fields | {"principal": self.principal})).model_dump(mode="json")

    def preparation_fields(self, plan_id, version, **ref):
        with self.records.store._transaction() as c:
            value = self.records._read(c, self.principal, plan_id, version, ref)
            if _get(c, plan_id, _slot(version) + ":execution"):
                raise EventError("initial_execution_already_claimed")
            return self._fields(value, ref)

    def _result(self, intent, value, envelope):
        try:
            result = self.client.validate(intent, value, envelope)
        except Exception:
            raise EventError("initial_execution_unconfirmed") from None
        return self._checked(intent, value, result)

    @staticmethod
    def _checked(intent, value, result):
        try:
            result = _validated(InitialResult, result)
            if result.schema_valid is not True or result.lint_passed is not True:
                raise ValueError()
            if (result.request_id, result.input_digest, result.runtime_source_sha) != (
                    intent.request_id, intent.input_digest, intent.runtime_source_sha):
                raise ValueError()
            proposal = result.proposal
            if (proposal.run_id, proposal.source_sha, digest(proposal)) != (
                    value.run_id, value.binding.source_sha, result.proposal_digest):
                raise ValueError()
            for edit in proposal.edits:
                if (edit.path not in value.manifest.editable_paths
                        or edit.before_sha256 != value.manifest.files[edit.path]):
                    raise ValueError()
        except Exception:
            raise EventError("initial_execution_unconfirmed") from None
        return result

    def execute(self, plan_id, version, **ref):
        return self._run(plan_id, version, ref, claim=True)

    def read(self, plan_id, version, **ref):
        return self._run(plan_id, version, ref, claim=False)

    def _run(self, plan_id, version, ref, *, claim):
        slot = _slot(version) + ":execution"
        with self.records.store._transaction(write=True, plan_id=plan_id) as c:
            value = self.records._read(c, self.principal, plan_id, version, ref)
            old = _get(c, plan_id, slot)
            first = old is None
            if first and not claim:
                raise EventError("initial_execution_not_started")
            fields = self._fields(value, ref)
            try:
                key = self.client.recipient(fields["request_id"]) if first else old[0]["recipient_public_key"]
                intent = InitialIntent(**fields, recipient_public_key=key)
            except Exception:
                raise EventError("initial_recipient_unavailable") from None
            request = intent.model_dump(mode="json")
            if first:
                # Key preparation is a host callback. It can outlive authority or
                # source validity; do not commit a claim or submit from that read.
                self.records._read(c, self.principal, plan_id, version, ref)
                _put(c, plan_id, slot, request, {"state": "CLAIMED"})
            elif old[0] != request:
                raise EventError("initial_execution_conflict")
            saved = _get(c, plan_id, slot + ":result")
            if saved:
                if saved[0] != request:
                    raise EventError("initial_execution_conflict")
                return self._checked(intent, value, saved[1]).model_dump(mode="json")
        try:
            envelope = (self.client.submit if first else self.client.read)(intent)
        except ExecutionPending:
            raise ExecutionPending("initial_execution_pending") from None
        except ExecutionUnknown:
            raise ExecutionUnknown("initial_execution_unknown") from None
        except Exception:
            raise EventError("initial_execution_unconfirmed") from None
        with self.records.store._transaction(write=True, plan_id=plan_id) as c:
            value = self.records._read(c, self.principal, plan_id, version, ref)
            result = self._result(intent, value, envelope).model_dump(mode="json")
            self.records._read(c, self.principal, plan_id, version, ref)
            saved = _get(c, plan_id, slot + ":result")
            if saved and saved != (request, result):
                raise EventError("initial_execution_conflict")
            if not saved:
                _put(c, plan_id, slot + ":result", request, result)
            return result

    def stored(self, plan_id, version, **ref):
        """Read the same durable Proposal in PROPOSE or its immediate REHEARSE.

        No client method is called. The journal holds only the authenticated,
        normalized result. The T15 host must bind its own Admission to this plan,
        source, task and published Change before consuming it.
        """
        with self.records.store._transaction() as c:
            return self._stored_result(c, plan_id, version, ref, promoted=True)[2].model_dump(mode="json")

    def _stored_result(self, c, plan_id, version, ref, *, promoted=False):
        value = self.records._read(c, self.principal, plan_id, version, ref, promoted=promoted)
        saved = _get(c, plan_id, _slot(version) + ":execution:result")
        if not saved:
            raise EventError("initial_execution_not_complete")
        intent = InitialIntent(**saved[0])
        if self._fields(value, ref) != intent.model_dump(exclude={"recipient_public_key"}):
            raise EventError("initial_execution_conflict")
        result = self._checked(intent, value, saved[1])
        publication = _get(c, plan_id, _slot(version) + ":publication")
        if publication:
            published = _validated(CandidatePublication, publication[1])
            if (published.request_id, published.plan_digest, published.source_sha, published.proposal_digest) != (
                    intent.request_id, value.binding.plan_digest, value.binding.source_sha, result.proposal_digest):
                raise EventError("initial_publication_unconfirmed")
        return value, intent, result

    def rehearsal_run_id(self, c, plan, task_id, task):
        """RehearsalService.first_run: current stored Proposal, after real publication.

        Called in the Admission transaction. No model/provider methods are used.
        The ordinary T10 REHEARSE checks and commit-before-submit still apply.
        """
        return self._rehearsal_result(c, plan, task_id, task).proposal.run_id

    def _rehearsal_result(self, c, plan, task_id, task):
        version, plan_id = plan["version"] - 1, plan["plan_id"]
        if plan["state"] != "REHEARSE":
            raise EventError("initial_rehearsal_unavailable")
        admitted = _get(c, plan_id, _slot(version))
        if admitted is None:
            raise EventError("initial_rehearsal_unavailable")
        value, _, result = self._stored_result(c, plan_id, version, admitted[1], promoted=True)
        if (value.binding.tenant_id != plan["tenant_id"] or value.binding.task_id != task_id
                or (value.binding.source_sha, value.binding.task_digest) != (task.source_sha, task.task_digest)):
            raise EventError("initial_rehearsal_unavailable")
        return result

    def rehearsal_result(self, admission):
        """Existing T15 result view, only for this genuinely committed first Admission.

        The initial input is never interpreted as an Admission. No provider/model
        call occurs here; retry attempts must use their own proposal path.
        """
        from .rehearsal import Admission, RehearsalTask, admitted_attempt

        if type(admission) is not Admission or type(admission.attempt) is not int or admission.attempt != 1:
            raise EventError("initial_rehearsal_unavailable")
        with self.records.store._transaction(write=True, plan_id=admission.plan_id) as c:
            row = c.execute("SELECT * FROM plans WHERE plan_id=?", (admission.plan_id,)).fetchone()
            if (row is None or (row["tenant_id"], row["version"]) != (admission.tenant_id, admission.plan_version)
                    or admitted_attempt(c, admission.plan_id, admission.rehearsal_id, admission.attempt) != admission):
                raise EventError("initial_rehearsal_unavailable")
            result = self._rehearsal_result(c, dict(row), admission.task_id,
                RehearsalTask(admission.source_sha, admission.task_digest))
            if result.proposal.run_id != admission.rehearsal_id:
                raise EventError("initial_rehearsal_unavailable")
            return dict(state="PROPOSAL_READY", attempt=admission.attempt,
                run_id=admission.rehearsal_id, source_sha=admission.source_sha,
                proposal=result.proposal.model_dump(mode="json"), proposal_digest=result.proposal_digest)
    def publication_inputs(self, plan_id, version, **ref):
        """Read authorized, saved inputs for the operator's T7 evidence lookup.

        No client, network or mutation. Only PROPOSE is accepted. The eventual
        evidence_reader rechecks current authority after this lookup completes.
        """
        with self.records.store._transaction() as c:
            return self._stored_result(c, plan_id, version, ref)

    def evidence_reader(self, *, publication):
        """WorkflowRecovery read port. Publication is a trusted read-only adapter.

        Called under the existing plan lock. It must authenticate T7 provenance,
        mapping to the saved proposal and exact diff bytes; no build is started.
        """
        if not callable(publication):
            raise ValueError("publication_reader_required")

        def read(c, plan, previous, run):
            version, plan_id = plan["version"], plan["plan_id"]
            admitted = _get(c, plan_id, _slot(version))
            if not admitted:
                raise EventError("initial_execution_not_complete")
            value = self.records._read(c, self.principal, plan_id, version, admitted[1])
            saved = _get(c, plan_id, _slot(version) + ":execution:result")
            if not saved:
                raise EventError("initial_execution_not_complete")
            intent = InitialIntent(**saved[0])
            if self._fields(value, admitted[1]) != intent.model_dump(exclude={"recipient_public_key"}):
                raise EventError("initial_execution_conflict")
            result = self._checked(intent, value, saved[1])
            try:
                published = _validated(CandidatePublication, publication(c, value, intent, result))
                if (published.request_id, published.plan_digest, published.source_sha, published.proposal_digest) != (
                        intent.request_id, value.binding.plan_digest, value.binding.source_sha, result.proposal_digest):
                    raise ValueError()
            except Exception:
                raise EventError("initial_publication_unconfirmed") from None
            self.records._read(c, self.principal, plan_id, version, admitted[1])
            key = _slot(version) + ":publication"
            receipt = published.model_dump(mode="json")
            old = _get(c, plan_id, key)
            if old and old != ({}, receipt):
                raise EventError("initial_publication_conflict")
            if not old:
                _put(c, plan_id, key, {}, receipt)
            return self.records._proof(value, receipt)

        return read
