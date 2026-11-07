"""Environment kinds, roles and adapter readiness for registration (ADR-0034).

The contract is docs/contracts/environment-kinds.json. This module is the one
place registration code reads kinds, roles and adapter slot names from; a test
keeps it equal to the contract.
"""

import json
from importlib import resources
from typing import Literal, get_args

Kind = Literal["aws", "gcp", "onprem"]
Role = Literal["source", "target"]
KINDS: tuple[str, ...] = get_args(Kind)
ROLES: tuple[str, ...] = get_args(Role)

SLOTS: tuple[str, ...] = (
    "source.static_rules",
    "source.deploy_trace",
    "source.neutral_export",
    "source.exit_scope",
    "target.catalog",
    "target.neutral_apply",
    "target.rehearsal",
    "target.build",
    "both.control_probes",
    "both.identity",
)
REJECTION_PREFIX = "adapter_missing:"

# Deprecated: catalogs written for the P0 aws -> onprem pair omit catalog_role.
# New catalogs must set it; gcp (and any later kind) has no implied role.
LEGACY_DEFAULT_ROLES = {"aws": "source", "onprem": "target"}


def _validated(table):
    if type(table) is not dict or not set(table) <= set(KINDS):
        raise ValueError("invalid_adapter_readiness")
    out = {}
    for kind, slots in table.items():
        if type(slots) is not list or len(set(slots)) != len(slots) or not set(slots) <= set(SLOTS):
            raise ValueError("invalid_adapter_readiness")
        out[kind] = frozenset(slots)
    return out


def default_readiness():
    """Slots implemented in this repository. Update the data file as A3-A10 merge."""
    data = json.loads(resources.files(__package__).joinpath("adapter_readiness.json").read_text())
    if data.get("schema_version") != "bravo.adapter-readiness/v1":
        raise ValueError("invalid_adapter_readiness")
    return _validated(data["kinds"])


def adapter_readiness(override=None):
    """Packaged defaults, with operator-provided kinds replacing whole entries."""
    table = {kind: frozenset() for kind in KINDS} | default_readiness()
    if override is not None:
        table |= _validated(override)
    return table


def missing_slot(readiness, source_kind, target_kind):
    """First slot, in contract order, that blocks this pair; None when ready."""
    for slot in SLOTS:
        side = slot.split(".")[0]
        kinds = {"source": (source_kind,), "target": (target_kind,)}.get(side, (source_kind, target_kind))
        if any(slot not in readiness.get(kind, ()) for kind in kinds):
            return slot
    return None
