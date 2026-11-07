"""The planning port an agent run receives: change one module's params against the committed state.

The trusted control path builds it from the committed desired state and hands it to the agent's
tool layer (T11 ``infra.plan``). The agent never sees Git, credentials or a cluster, and every
plan is ``origin=agent``: widening unauthenticated access and changing the module set are refused.
"""

import json

from .catalog import check_state, module
from .modules import IacError
from .plan import digest, plan


class AgentPlanner:
    def __init__(self, state):
        self.state = check_state(state)
        self.digest = digest(self.state)
        self.environment = self.state["environment"]

    def describe(self) -> str:
        """Code-owned tool description: modules, their params and the committed values (no secrets)."""
        lines = [f"Destination IaC for {self.environment} (state digest {self.digest[:16]}). "
                 "Plan one module's params; the result is a proposal for human review and never applies. "
                 "Widening unauthenticated access is refused."]
        for entry in self.state["modules"]:
            found = module(entry["id"], entry["version"])
            lines.append(f"- {found.id} v{found.version}: {found.description} "
                         f"params {json.dumps(found.params, sort_keys=True)}; "
                         f"committed {json.dumps(entry['params'], sort_keys=True)}")
        return "\n".join(lines)

    def plan(self, module_id, params) -> dict:
        if not any(entry["id"] == module_id for entry in self.state["modules"]):
            raise IacError("IAC_MODULE_NOT_IN_ENVIRONMENT")
        proposed = dict(self.state, modules=[dict(entry, params=params) if entry["id"] == module_id else entry
                                             for entry in self.state["modules"]])
        return plan(self.state, proposed, origin="agent")
