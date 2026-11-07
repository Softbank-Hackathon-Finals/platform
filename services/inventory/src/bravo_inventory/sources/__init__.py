"""Source-side adapters per environment kind (ADR-0034 slots ``source.static_rules`` and
``source.deploy_trace``).

``deploy_adapter_for(kind, exports, live=...)`` returns the registered ``DeployAdapter`` or an
``Unsupported`` value with status ``UNSUPPORTED``; it never falls back to another kind's adapter.
The static rules registry is ``bravo_inventory.source_rules.STATIC_RULES``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..deploy_trace import DeployAdapter
from ..source_rules import STATIC_RULES, UNSUPPORTED, Unsupported, static_rules_for
from .aws import Boto3Adapter, FileAdapter
from .gcp import GcloudAdapter, GcpFileAdapter
from .onprem import OnpremFileAdapter

DEPLOY_TRACE_SLOT = "source.deploy_trace"
STATIC_RULES_SLOT = "source.static_rules"

# kind → mode → factory(exports directory). ``file`` reads exports only; ``live`` also reads the
# cloud through a read-only allowlist.
DEPLOY_ADAPTERS: dict[str, dict[str, Callable[[Path], DeployAdapter]]] = {
    "aws": {"file": FileAdapter, "live": Boto3Adapter},
    "gcp": {"file": GcpFileAdapter, "live": GcloudAdapter},
    "onprem": {"file": OnpremFileAdapter},
}


def deploy_adapter_for(kind: str, exports: Path, *, live: bool = False) -> DeployAdapter | Unsupported:
    factory = DEPLOY_ADAPTERS.get(kind, {}).get("live" if live else "file")
    if factory is None:
        return Unsupported(DEPLOY_TRACE_SLOT + (" (live)" if live else ""), kind)
    return factory(Path(exports))


def slot_status(kind: str) -> dict[str, str]:
    """``{slot: "ok" | "UNSUPPORTED"}`` for the source slots this service fills."""
    return {
        STATIC_RULES_SLOT: "ok" if kind in STATIC_RULES else UNSUPPORTED,
        DEPLOY_TRACE_SLOT: "ok" if "file" in DEPLOY_ADAPTERS.get(kind, {}) else UNSUPPORTED,
    }


__all__ = [
    "DEPLOY_ADAPTERS",
    "DEPLOY_TRACE_SLOT",
    "STATIC_RULES_SLOT",
    "UNSUPPORTED",
    "Unsupported",
    "deploy_adapter_for",
    "slot_status",
    "static_rules_for",
]
