"""One durable T10 intent per Admission; ambiguous Actions effects are read only."""

import math
import re
import time
from dataclasses import asdict

from bravo_agent.contracts import digest
from bravo_agent.product_execution_contracts import (
    ExecutionIntent,
    ExecutionPending,
    ExecutionUnknown,
    validate_execution_receipt,
)

from .cutover import _get, _put
from .product_inputs import ProductInputRecords, _admission, _validated
from .rehearsal import Admission
from .store import EventError


class ProductExecution:
    """Trusted host port; uses the existing operator dispatcher, never Pod tokens.

    The caller registers and authorizes the product input before execute(). The
    operator client's separate journal binds the one external workflow dispatch
    to its actual actor/run/artifact. This journal binds that intent to T10's
    current Admission. Neither an input reference nor this object grants access.
    """

    def __init__(self, records, client, *, principal, runtime_source_sha):
        if (type(records) is not ProductInputRecords
                or any(not callable(getattr(client, name, None)) for name in ("recipient", "submit", "read"))
                or not isinstance(principal, str) or re.fullmatch(r"[a-f0-9]{64}", principal) is None
                or not isinstance(runtime_source_sha, str)
                or re.fullmatch(r"[a-f0-9]{40}", runtime_source_sha) is None):
            raise ValueError("registered_product_execution_ports_required")
        self.records, self.client = records, client
        self.principal, self.runtime_source_sha = principal, runtime_source_sha

    def _binding(self, c, admission, input_ref, input_digest):
        record = self.records._read_locked(c, admission.plan_id, self.principal, input_ref)
        if record["admission"] != asdict(admission) or record["input_digest"] != input_digest:
            raise EventError("product_execution_mismatch")
        request_id = "t10-" + digest(dict(admission=asdict(admission), input_ref=input_ref,
            input_digest=input_digest, principal=self.principal, runtime_source_sha=self.runtime_source_sha))
        fields = dict(request_id=request_id, input_ref=input_ref, input_digest=input_digest,
            admission_digest=digest(asdict(admission)), runtime_source_sha=self.runtime_source_sha, mode="propose")
        return fields, _validated(record["product_input"])

    @staticmethod
    def _result(intent, value, envelope):
        try:
            # The operator client verifies the actual API run/artifact before returning this envelope.
            validate_execution_receipt(intent, envelope["github_run_id"], envelope)
            result = envelope["result"]
            if (result["input"] != value.bundle.model_dump(mode="json")
                    or result["snapshot_digest"] != digest(value.manifest)
                    or result["run_id"] != value.bundle.run_id
                    or result["source_sha"] != value.bundle.source_sha):
                raise ValueError()
        except Exception:
            raise EventError("product_execution_unconfirmed") from None
        return result

    def execute(self, admission, *, input_ref, input_digest):
        """Commit intent first. Only the invocation creating it may dispatch once."""
        return self._run(admission, input_ref, input_digest, allow_claim=True)

    def preparation_fields(self, admission, *, input_ref, input_digest):
        """Read six pre-key fields without claiming or calling the client.

        This is not admission to dispatch: execute() rechecks current authority
        and commits the intent. Claimed attempts keep their existing recovery
        path; this method never prepares a replacement key or mailbox for them.
        """
        _admission(admission)
        with self.records.store._transaction() as c:
            fields, _value = self._binding(c, admission, input_ref, input_digest)
            if _get(c, admission.plan_id, _execution_slot(admission)) is not None:
                raise EventError("product_execution_already_claimed")
            return fields

    def read(self, admission, *, input_ref, input_digest):
        """Recover the same result; never create an intent or dispatch a workflow."""
        return self._run(admission, input_ref, input_digest, allow_claim=False)

    def _run(self, admission, input_ref, input_digest, *, allow_claim):
        _admission(admission)
        slot = _execution_slot(admission)
        with self.records.store._transaction(write=True, plan_id=admission.plan_id) as c:
            fields, value = self._binding(c, admission, input_ref, input_digest)
            old = _get(c, admission.plan_id, slot)
            first = old is None
            if first and not allow_claim:
                raise EventError("product_execution_not_started")
            try:
                # Only the first claim may prepare local key material. Replays keep
                # the original public key even when the operator's key is missing.
                recipient = (self.client.recipient(fields["request_id"]) if first
                             else old[0]["recipient_public_key"])
                intent = ExecutionIntent(**fields, recipient_public_key=recipient)
            except Exception:
                raise EventError("product_recipient_unavailable") from None
            request = intent.model_dump(mode="json") | {"principal": self.principal}
            if first:
                _put(c, admission.plan_id, slot, request, {"state": "CLAIMED", "intent": intent.model_dump(mode="json")})
            elif old[0] != request:
                raise EventError("product_execution_conflict")
            saved = _get(c, admission.plan_id, slot + ":result")
            if saved:
                if saved[0] != request:
                    raise EventError("product_execution_conflict")
                return self._result(intent, value, saved[1])
        try:
            envelope = (self.client.submit if first else self.client.read)(intent)
        except ExecutionPending:
            raise ExecutionPending("product_execution_pending") from None
        except ExecutionUnknown:
            raise ExecutionUnknown("product_execution_unknown") from None
        except Exception:
            # Includes response loss and a crash after the claim but before submit.
            # The durable claim remains, so every following call only reads.
            raise EventError("product_execution_unconfirmed") from None
        self._result(intent, value, envelope)
        with self.records.store._transaction(write=True, plan_id=admission.plan_id) as c:
            fields, value = self._binding(c, admission, input_ref, input_digest)
            current = ExecutionIntent(**fields, recipient_public_key=intent.recipient_public_key)
            if current != intent:
                raise EventError("product_execution_conflict")
            result = self._result(intent, value, envelope)
            old = _get(c, admission.plan_id, slot + ":result")
            if old:
                if old != (request, envelope):
                    raise EventError("product_execution_conflict")
            else:
                _put(c, admission.plan_id, slot + ":result", request, envelope)
            return result


def _execution_slot(admission):
    return "product-execution:" + admission.rehearsal_id + ":1"


class ActionsProductCall:
    """Bounded T15 call port: wait by reading the same intent, never redispatch.

    This is an operator-host adapter. It does not give a Kubernetes Pod GitHub
    credentials. RemoteProposalSource must explicitly support the reference-only
    request before a deployment can install this port.
    """

    def __init__(self, execution, *, wait_seconds=900, poll_seconds=5, clock=time.monotonic, sleep=time.sleep):
        if (type(execution) is not ProductExecution or not callable(clock) or not callable(sleep)
                or any(type(v) not in (int, float) or not math.isfinite(v)
                       for v in (wait_seconds, poll_seconds))
                or not 1 <= wait_seconds <= 1800 or not 0.1 <= poll_seconds <= 30):
            raise ValueError("bounded_product_wait_required")
        self.execution = execution
        self.wait_seconds, self.poll_seconds = wait_seconds, poll_seconds
        self.clock, self.sleep = clock, sleep

    def __call__(self, body):
        if type(body) is not dict or set(body) != {"input_ref", "input_digest"}:
            raise EventError("product_execution_mismatch")
        record = self.execution.records.read(self.execution.principal, body["input_ref"])
        admission = Admission(**record["admission"])
        deadline = self.clock() + self.wait_seconds
        operation = self.execution.execute
        while True:
            try:
                return 200, operation(admission, **body)
            except ExecutionPending:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise EventError("product_execution_unconfirmed") from None
                self.sleep(min(self.poll_seconds, remaining))
                operation = self.execution.read
            except ExecutionUnknown:
                raise EventError("product_execution_unconfirmed") from None
