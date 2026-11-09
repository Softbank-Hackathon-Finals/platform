"""T44 destination IaC: module catalog, desired state in Git and deterministic plans.

Read and propose only. Applying stays with GitOps: a reviewed, merged change to the desired
state is what an applier acts on. An agent may plan, never apply.
"""

from .catalog import CATALOG, check_state, describe, module
from .modules import IacError, Module
from .plan import digest, plan

__all__ = ["CATALOG", "IacError", "Module", "check_state", "describe", "digest", "module", "plan"]
