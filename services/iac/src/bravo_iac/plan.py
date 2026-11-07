"""Deterministic plan between the desired state in Git and a proposed one.

The plan is a proposal, never an execution: it lists the resource shapes that would change,
the restarts they trigger and what unauthenticated access becomes. The same inputs always give
the same plan_digest. An agent may not widen exposure or change which modules an environment
has; an operator may (the demo's planted defect is set that way), and the plan records who asked.
"""

import hashlib
import json

from .catalog import check_state, module
from .modules import IacError

ORIGINS = ("agent", "operator")


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _resources(entry) -> dict:
    if entry is None:
        return {}
    found = module(entry["id"], entry["version"])
    return {f'{r["kind"]}/{r["namespace"]}/{r["name"]}': r for r in found.render(entry["params"])}


def _exposure(entry) -> dict:
    if entry is None:
        return {"anonymous": [], "checks": []}
    return module(entry["id"], entry["version"]).exposure(entry["params"])


def plan(current, proposed, *, origin: str) -> dict:
    if origin not in ORIGINS:
        raise IacError("IAC_ORIGIN_INVALID")
    cur, new = check_state(current), check_state(proposed)
    if cur["environment"] != new["environment"]:
        raise IacError("IAC_ENVIRONMENT_MISMATCH")
    before = {m["id"]: m for m in cur["modules"]}
    after = {m["id"]: m for m in new["modules"]}
    refusals, changes, effects, widens = [], [], [], False
    if origin == "agent" and set(before) != set(after):
        refusals.append("IAC_MODULE_SET_CHANGE_REFUSED")
    for module_id in sorted(set(before) | set(after)):
        old, fresh = _resources(before.get(module_id)), _resources(after.get(module_id))
        for key in sorted(set(old) | set(fresh)):
            a, b = old.get(key), fresh.get(key)
            if a == b:
                continue
            changes.append({
                "module": module_id, "resource": key,
                "action": "create" if a is None else "delete" if b is None else "update",
                "before": a and a["shape"], "after": b and b["shape"],
                "secret_inputs": (b or a).get("secret_inputs", []),
                "restarts": (b or a).get("restart_on_change", []),
            })
        was, now = _exposure(before.get(module_id)), _exposure(after.get(module_id))
        if was["anonymous"] != now["anonymous"]:
            effects.append({"module": module_id, "anonymous_access": {"before": was["anonymous"],
                                                                       "after": now["anonymous"]},
                            "checks": now["checks"]})
        reach_was, reach_now = set(was.get("reach", [])), set(now.get("reach", []))
        if reach_was != reach_now:
            effects.append({"module": module_id, "reach": {"added": sorted(reach_now - reach_was),
                                                           "removed": sorted(reach_was - reach_now)}})
        widens = widens or not set(now["anonymous"]) <= set(was["anonymous"]) or not reach_now <= reach_was
    if origin == "agent" and widens:
        refusals.append("IAC_WIDENING_REFUSED")
    grants = sorted({json.dumps(g, sort_keys=True) for m in new["modules"]
                     for g in module(m["id"], m["version"]).grants(m["params"])})
    doc = {
        "kind": "iac-plan.v1", "environment": cur["environment"], "origin": origin,
        "base_digest": digest(cur), "proposed_digest": digest(new),
        "changes": changes, "effects": effects, "widens": widens,
        "refusals": refusals, "accepted": not refusals,
        "tailnet_grants": [json.loads(g) for g in grants],  # what the tailnet policy must allow
        # A plan is not an approval and never applies anything by itself.
        "authorizes_apply": False,
    }
    doc["plan_digest"] = digest(doc)
    return doc
