"""T10 half of the initial AI proposal, run inside the platform (Reader or workflow host Pod).

The Job carrier and the recipient key stay with the operator (personal kubeconfig, operator ledger);
the T10 database stays in the cluster. The operator script relays between them, one JSON request on
stdin, one JSON answer on stdout:

    {"op": "fields"|"claim"|"store"|"fence", "plan_id", "version", "ref": {...},
     "principal", "runtime_source_sha", ...}

fields  preparation fields only (no claim).
claim   commit the CLAIMED marker with the operator's recipient public key; nothing is sent here.
store   T10 rechecks the operator-opened, already authenticated result (schema, lint, run, source,
        editable paths, base digests) and stores it once.
fence   record the operator ledger's unreceived-fence proof (see InitialProposalExecution).
"""

import json
import sys

from bravo_agent.product_execution_contracts import ExecutionPending

from .initial_proposal import InitialProposalExecution
from .store import EventError

OPS = {"fields", "claim", "store", "fence"}


class _Relay:
    """The operator already did the carrier work; this client only hands T10 its answers."""

    def __init__(self, request):
        self.request = request

    def recipient(self, request_id):
        key = self.request.get("recipient_public_key")
        if not isinstance(key, str):
            raise ValueError("recipient_required")
        return key

    def submit(self, intent):
        raise ExecutionPending("operator_submits_the_job")  # the claim is committed before this

    def read(self, intent):
        envelope = self.request.get("envelope")
        if envelope is None:
            raise ExecutionPending("operator_result_not_supplied")
        return envelope

    def validate(self, intent, value, envelope):
        # The operator opened the sealed receipt; T10 rechecks its binding to this exact request,
        # Job execution id and status, then the content checks of InitialProposalExecution run.
        from .initial_proposal_job import execution_id
        from .initial_proposal_transport import _validated_result, validate_initial_receipt

        receipt = validate_initial_receipt(intent, execution_id(intent.request_id), envelope)
        return _validated_result(intent, value, receipt)

    def fence_unreceived(self, intent):
        return self.request["proof"]


def handle(records, request):
    if type(request) is not dict or request.get("op") not in OPS:
        raise ValueError("relay_request_invalid")
    execution = InitialProposalExecution(records, _Relay(request), principal=request["principal"],
                                         runtime_source_sha=request["runtime_source_sha"])
    plan_id, version, ref = request["plan_id"], request["version"], request["ref"]
    op = request["op"]
    if op == "fields":
        return {"fields": execution.preparation_fields(plan_id, version, **ref)}
    if op == "claim":
        try:
            execution.execute(plan_id, version, **ref)
        except ExecutionPending:
            pass
        return {"claimed": True}
    if op == "store":
        return {"result": execution.read(plan_id, version, **ref)}
    return {"proof": execution.fence_unreceived(plan_id, version, **ref)}


def main():
    from .postgres import PostgresPlanStore
    from .product_read_server import configured_initial_reader
    from .window_server import runtime_connection

    try:
        request = json.loads(sys.stdin.read(1024 * 1024))
        store = PostgresPlanStore(runtime_connection())
        reader = configured_initial_reader(store, request.pop("reader_config", "/product-read/reader.json"))
        print(json.dumps({"status": "PASS", **handle(reader.records, request)}, sort_keys=True))
    except EventError as error:
        print(json.dumps({"status": "HOLD", "code": error.code}))
        raise SystemExit(10) from None
    except Exception as error:  # noqa: BLE001 - the operator reads one bounded code, never a trace
        print(json.dumps({"status": "FAIL", "reason": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
