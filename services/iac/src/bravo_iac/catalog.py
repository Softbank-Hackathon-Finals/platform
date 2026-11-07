"""The module catalog and the desired-state document of one environment."""

import re

from .modules import (
    API_TRANSPORT_ACCESS,
    OPERATOR_ACCESS,
    TAILNET_EGRESS,
    TAILNET_INGRESS,
    TAILNET_OPERATOR,
    TARGET_OBJECT_EXPOSURE,
    IacError,
    Module,
)
from .node import CLUSTER_AUDIT, SERVICE_ACCOUNT_ISSUER

CATALOG: dict[tuple[str, str], Module] = {
    (m.id, m.version): m for m in (TARGET_OBJECT_EXPOSURE, TAILNET_EGRESS, TAILNET_INGRESS, TAILNET_OPERATOR, OPERATOR_ACCESS,
                                   API_TRANSPORT_ACCESS, CLUSTER_AUDIT, SERVICE_ACCOUNT_ISSUER)
}
SCHEMA_VERSION = "1"
ENVIRONMENT_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")


def module(module_id, version) -> Module:
    found = CATALOG.get((module_id, version)) if isinstance(module_id, str) and isinstance(version, str) else None
    if found is None:
        raise IacError("IAC_MODULE_UNKNOWN")
    return found


def describe() -> list[dict]:
    return [{"id": m.id, "version": m.version, "description": m.description, "params": m.params}
            for m in CATALOG.values()]


def check_state(doc) -> dict:
    """A desired state: {schema_version, environment, modules: [{id, version, params}]}, one entry per id."""
    if (type(doc) is not dict or set(doc) != {"schema_version", "environment", "modules"}
            or doc["schema_version"] != SCHEMA_VERSION or not isinstance(doc["environment"], str)
            or not ENVIRONMENT_RE.fullmatch(doc["environment"]) or type(doc["modules"]) is not list):
        raise IacError("IAC_STATE_INVALID")
    seen, entries = set(), []
    for entry in doc["modules"]:
        if type(entry) is not dict or set(entry) != {"id", "version", "params"}:
            raise IacError("IAC_STATE_INVALID")
        found = module(entry["id"], entry["version"])
        if found.id in seen:
            raise IacError("IAC_MODULE_DUPLICATE")
        seen.add(found.id)
        entries.append({"id": found.id, "version": found.version, "params": found.check(entry["params"])})
    # One proxy is either the auth proxy people impersonate through or a transport proxy, never both.
    tags = {e["id"]: e["params"]["proxy_tag"] for e in entries if e["id"] in {"operator-access", "api-transport-access"}}
    if len(tags) == 2 and len(set(tags.values())) == 1:
        raise IacError("IAC_PARAMS_INVALID")
    return {"schema_version": SCHEMA_VERSION, "environment": doc["environment"], "modules": entries}
