"""T16: dependency and configuration inventory for a registered workload.

Deterministic. No LLM import anywhere in this package; ambiguous items are only *marked*
for the agent (T11) to classify, they are never classified here by a model.
"""

from .report import build_inventory, validate_inventory

__all__ = ["build_inventory", "validate_inventory"]
