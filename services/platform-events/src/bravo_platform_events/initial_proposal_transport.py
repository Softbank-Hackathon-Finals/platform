"""T38's operator/mailbox adapters for T10's canonical initial Proposal contract.

No HTTP endpoint, model conversion or scheduler is installed here. The operator reads
the sealed receipt from the platform Job and delivers it to the owner-only mailbox.
"""

import json
import re

from bravo_agent.contracts import digest
from bravo_agent.product_execution_contracts import ExecutionUnknown
from bravo_agent.product_execution_mailbox import FileProductExecution
from bravo_agent.product_ledger import OperatorLedger
from bravo_agent.subscription_product_seal import open_bound_receipt, seal_bound_receipt

from .initial_proposal import InitialInput, InitialIntent, InitialResult


def _intent(value):
    # Preserve raw scalar types: model_dump may normalize an unchecked bool to 1.
    raw = dict(value) if isinstance(value, InitialIntent) else value
    return InitialIntent.model_validate_json(json.dumps(raw, allow_nan=False))


def initial_receipt_binding(intent, run_id):
    intent = _intent(intent)
    if not isinstance(run_id, (str, int)) or type(run_id) is bool or not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)):
        raise ValueError("initial execution run required")
    return dict(schema_version=1, purpose=intent.purpose, request_id=intent.request_id,
        plan_id=intent.plan_id, plan_version=intent.plan_version,
        input_ref_digest=digest(intent.input_ref), input_digest=intent.input_digest,
        binding_digest=intent.binding_digest, runtime_source_sha=intent.runtime_source_sha,
        workflow_sha=intent.runtime_source_sha, github_run_id=str(run_id), github_run_attempt=1,
        recipient_key_digest=digest(intent.recipient_public_key))


def validate_initial_receipt(intent, run_id, receipt):
    """Check binding after provider authentication; shape alone is not provenance."""
    intent = _intent(intent)
    expected = initial_receipt_binding(intent, run_id)
    if (type(receipt) is not dict or set(receipt) != set(expected) | {"status", "cleanup_confirmed", "result"}
            or any(type(receipt[key]) is not type(value) or receipt[key] != value for key, value in expected.items())
            or type(receipt["status"]) is not int or receipt["status"] != 200
            or receipt["cleanup_confirmed"] is not True):
        raise ValueError("initial execution unconfirmed")
    result = InitialResult.model_validate_json(json.dumps(receipt["result"], allow_nan=False))
    if ((result.request_id, result.input_digest, result.runtime_source_sha) !=
            (intent.request_id, intent.input_digest, intent.runtime_source_sha)
            or result.execution_receipt_id != f"github-{run_id}-1"
            or result.proposal_digest != digest(result.proposal)
            or result.schema_valid is not True or result.lint_passed is not True):
        raise ValueError("initial result unconfirmed")
    return receipt


def seal_initial_receipt(intent, run_id, receipt):
    intent = _intent(intent)
    validate_initial_receipt(intent, run_id, receipt)
    return seal_bound_receipt(intent.recipient_public_key, initial_receipt_binding(intent, run_id), receipt)


def open_initial_receipt(intent, run_id, sealed, private):
    intent = _intent(intent)
    return open_bound_receipt(intent.recipient_public_key, initial_receipt_binding(intent, run_id), sealed, private)


def _validated_result(intent, value, receipt):
    """The delivered result for exactly this intent's input and the input's own run and source."""
    raw = value.model_dump(mode="json") if isinstance(value, InitialInput) else value
    value = InitialInput.model_validate_json(json.dumps(raw, allow_nan=False))
    if ((value.binding.plan_id, value.binding.plan_version, digest(value), digest(value.binding)) !=
            (intent.plan_id, intent.plan_version, intent.input_digest, intent.binding_digest)):
        raise ExecutionUnknown("INITIAL_INPUT_CONFLICT")
    result = receipt["result"]
    proposal = InitialResult.model_validate_json(json.dumps(result)).proposal
    if (proposal.run_id, proposal.source_sha) != (value.run_id, value.binding.source_sha):
        raise ExecutionUnknown("INITIAL_PROPOSAL_CONFLICT")
    return result


class OperatorInitialProposal(OperatorLedger):
    """The operator ledger with T10's initial intent and receipt contract.

    The carrier is the platform Job (initial_proposal_job). Receipt encryption keeps
    input/result contents out of the Job log.
    """

    _parse_intent = staticmethod(_intent)
    _open_receipt = staticmethod(open_initial_receipt)
    _validate_receipt = staticmethod(validate_initial_receipt)

    def validate(self, intent, value, envelope):
        """T10 port: the envelope must be what read() opens from this carrier right now."""
        intent = self._parse_intent(intent)
        if self.read(intent) != envelope:
            raise ExecutionUnknown("INITIAL_RECEIPT_CONFLICT")
        return _validated_result(intent, value, envelope)


class FileInitialProposal(FileProductExecution):
    """Trusted T10 host port; submit publishes locally, never dispatches a model.

    Only the authenticated operator may prepare/deliver through the existing
    owned-file carrier. HTTP/model callers cannot choose this directory or
    provide receipts. validate does no provider I/O and cannot deliver a result.
    """

    _parse_intent = staticmethod(_intent)

    @staticmethod
    def _receipt(intent, value):
        try:
            return validate_initial_receipt(intent, value["github_run_id"], value)
        except Exception:
            raise ExecutionUnknown("INITIAL_RECEIPT_UNCONFIRMED") from None

    def validate(self, intent, value, envelope):
        intent = self._bound(intent)
        # Compare with the immutable operator delivery, not a caller's assertion.
        if self.read(intent) != envelope:
            raise ExecutionUnknown("INITIAL_RECEIPT_CONFLICT")
        return _validated_result(intent, value, self._receipt(intent, envelope))
