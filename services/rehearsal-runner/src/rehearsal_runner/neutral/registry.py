"""Slot registry of this service (ADR-0034 §3.1, `docs/contracts/environment-kinds.json`).

rehearsal-runner fills `source.neutral_export`, `target.neutral_apply` and `target.rehearsal`.
A missing cell answers ``UNSUPPORTED``; the plan registration turns that into
``adapter_missing:<slot>`` and the gate treats it as UNKNOWN = block.
"""

from __future__ import annotations

from collections.abc import Mapping

from .apply import OBJECT_RENDERERS, SECRET_RENDERERS
from .export import OBJECT_EXPORTERS, SECRET_EXPORTERS
from .models import KINDS

UNSUPPORTED = "UNSUPPORTED"
READY = "READY"
PLAN_REJECTION_PREFIX = "adapter_missing:"
SOURCE_EXPORT, TARGET_APPLY, TARGET_REHEARSAL = "source.neutral_export", "target.neutral_apply", "target.rehearsal"
SLOTS = (SOURCE_EXPORT, TARGET_APPLY, TARGET_REHEARSAL)


class Unsupported(LookupError):
    """No adapter for (slot, kind). ``str()`` is the plan rejection reason."""

    def __init__(self, slot: str, kind: str):
        super().__init__(PLAN_REJECTION_PREFIX + slot)
        self.slot, self.kind = slot, kind


def cell(slot: str, kind: str, rehearsal: Mapping[str, str] | None = None) -> str:
    """READY or UNSUPPORTED for one (slot, kind). ``rehearsal`` is the `target.rehearsal` table
    (``rehearsal_runner.adapters``); this package does not import the rehearsal runtime."""
    if kind not in KINDS or slot not in SLOTS:
        return UNSUPPORTED
    if slot == SOURCE_EXPORT:
        return READY if kind in OBJECT_EXPORTERS and kind in SECRET_EXPORTERS else UNSUPPORTED
    if slot == TARGET_APPLY:
        return READY if kind in OBJECT_RENDERERS and kind in SECRET_RENDERERS else UNSUPPORTED
    return (rehearsal or {}).get(kind, UNSUPPORTED)


def readiness(source_kind: str, target_kind: str, rehearsal: Mapping[str, str] | None = None) -> dict[str, str]:
    """This service's cells for one (source, target) pair."""
    return {SOURCE_EXPORT: cell(SOURCE_EXPORT, source_kind), TARGET_APPLY: cell(TARGET_APPLY, target_kind),
            TARGET_REHEARSAL: cell(TARGET_REHEARSAL, target_kind, rehearsal)}


def rejections(source_kind: str, target_kind: str, rehearsal: Mapping[str, str] | None = None) -> list[str]:
    """``adapter_missing:<slot>`` for every missing cell, in slot order."""
    return [PLAN_REJECTION_PREFIX + slot for slot, value in readiness(source_kind, target_kind, rehearsal).items()
            if value == UNSUPPORTED]
