"""Read an already admitted initial input on the read-only product service.

The trusted admission host validates the original T16 document and source snapshot.
This reader cannot prepare an input: it only returns that immutable stored source.
InitialProposalRecords still checks the current plan, catalog, grant and digest.
"""

from bravo_agent.contracts import digest

from .cutover import _get
from .initial_proposal import InitialInput, _admission_key, logical_task_digest
from .store import EventError


def stored_initial_source(connection, plan, task):
    if plan["state"] not in ("PROPOSE", "REHEARSE"):
        raise EventError("initial_proposal_unavailable")
    # REHEARSE consumes its immediately preceding PROPOSE input. The enclosing
    # records reader still verifies the publication/checkpoint and current grant.
    version = plan["version"] - (plan["state"] == "REHEARSE")
    saved = _get(connection, plan["plan_id"], _admission_key(connection, plan["plan_id"], version))
    try:
        if saved is None:
            raise ValueError()
        value = InitialInput.model_validate(saved[0]["input"])
        binding = value.binding
        if ((binding.plan_id, binding.tenant_id, binding.plan_version, binding.task_digest,
             binding.source_sha) != (plan["plan_id"], plan["tenant_id"], version,
                                    logical_task_digest(task), task.source_sha)
                or digest(value) != saved[1]["input_digest"]):
            raise ValueError()
        return value.model_dump(mode="json", exclude={"binding", "run_id"})
    except Exception:
        raise EventError("initial_proposal_unavailable") from None
