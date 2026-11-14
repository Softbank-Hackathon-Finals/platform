"""ADR-0034 cells this service fills, for plan registration (`adapter_missing:<slot>`).

`source.neutral_export` and `target.neutral_apply` are READY for every kind. `target.rehearsal`
is READY only where the isolation profile and the credential flow both run: onprem today.
"""

from __future__ import annotations

from .bucket_job import BUCKET_JOBS
from .credential_targets import CREDENTIAL_TARGETS
from .isolation.profile import TARGET_PREFIXES
from .neutral import registry
from .neutral.models import KINDS


def rehearsal_table() -> dict[str, str]:
    return {kind: registry.READY if (kind in TARGET_PREFIXES and kind in BUCKET_JOBS
                                     and getattr(CREDENTIAL_TARGETS.get(kind), "runnable", False))
            else registry.UNSUPPORTED for kind in KINDS}


def readiness(source_kind: str, target_kind: str) -> dict[str, str]:
    return registry.readiness(source_kind, target_kind, rehearsal_table())


def rejections(source_kind: str, target_kind: str) -> list[str]:
    return registry.rejections(source_kind, target_kind, rehearsal_table())
