"""Read T33's existing summary; never infer a security PASS or gate permission."""

import asyncio
import re
from datetime import UTC, datetime

from .observations import ID, FixedJsonClient, ObservationError, _plan

# Existing T33 View.summary categories / finding.schema severity IDs.
CATEGORIES = ("IAM", "ENCRYPTION", "AUDIT", "EXPOSURE", "POLICY", "THREAT", "VULNERABILITY")
SEVERITIES = {"0", "1", "2", "3", "4", "5", "6", "99"}
MAX_COUNT = 2**53 - 1
# T33 auth.ID_PATTERN; T10 registration IDs remain the stricter ID below.
FINDINGS_ENV = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}$")


class T33Client(FixedJsonClient):
    async def summary(self, environment):
        if type(environment) is not str or not FINDINGS_ENV.fullmatch(environment):
            raise ObservationError("invalid_security_environment")
        return await self.read_json("/v1/security/summary", params={"environment_id": environment})


def project_summary(body, environment, now_ms):
    """Allowlisted presentation only. Failed/stale/missing counts are never zero."""
    unknown = {key: {"state": "UNKNOWN", "counts": None} for key in CATEGORIES}
    if (
        type(body) is not dict
        or body.get("categories") != list(CATEGORIES)
        or type(body.get("generated_time")) is not int
        or not 0 <= now_ms - body["generated_time"] <= 10_000
        or type(body.get("environments")) is not list
        or len(body["environments"]) != 1
    ):
        return unknown
    row = body["environments"][0]
    if (
        type(row) is not dict
        or row.get("environment_id") != environment
        or type(row.get("cells")) is not dict
        or set(row["cells"]) != set(CATEGORIES)
    ):
        return unknown
    projected = {}
    for category in CATEGORIES:
        cell = row["cells"][category]
        if type(cell) is not dict or cell.get("state") not in ("OK", "UNKNOWN", "STALE", "NONE"):
            return unknown
        state, counts, sources = cell["state"], cell.get("counts"), cell.get("sources")
        if (
            type(counts) is not dict
            or not set(counts) <= SEVERITIES
            or any(type(n) is not int or not 0 <= n <= MAX_COUNT for n in counts.values())
            or sum(counts.values()) > MAX_COUNT
            or type(sources) is not list
            or len(sources) > 32
            or any(type(s) is not str or not ID.fullmatch(s) for s in sources)
            or len(set(sources)) != len(sources)
            or (state == "NONE" and (counts or sources))
            or (state == "OK" and not sources)
        ):
            return unknown
        projected[category] = {"state": state, "counts": dict(counts) if state == "OK" else None}
    return projected


class PlanSecurity:
    def __init__(self, api, client, *, environments, authorize, clock=None):
        if (
            type(environments) is not dict
            or not environments
            or not all(
                type(k) is str and type(v) is str and ID.fullmatch(k) and FINDINGS_ENV.fullmatch(v)
                for k, v in environments.items()
            )
            or len(set(environments.values())) != len(environments)
            or not callable(authorize)
        ):
            raise ValueError("registered_security_mapping_and_permission_required")
        self.api, self.client = api, client
        self.environments, self.authorize = dict(environments), authorize
        self.clock = clock or (lambda: datetime.now(UTC))

    def _permit(self, scope, plan):
        # T33 is environment-wide. A plan viewer is not automatically entitled
        # to another application's findings in the same environment.
        if self.authorize(scope, plan) is not True:
            raise ObservationError("forbidden", 403)

    async def read(self, scope, plan_id):
        before = await _plan(self.api, scope, plan_id)
        self._permit(scope, before)
        refs = [before.get(side + "_environment_id") for side in ("source", "target")]
        if any(ref not in self.environments for ref in refs) or refs[0] == refs[1]:
            raise ObservationError("security_unconfigured")
        names = [self.environments[ref] for ref in refs]

        async def fetch(env):
            try:
                return await asyncio.wait_for(self.client.summary(env), timeout=5)
            except Exception:
                return None

        bodies = await asyncio.gather(*(fetch(env) for env in names))
        current = await _plan(self.api, scope, plan_id)
        self._permit(scope, current)
        if any(
            before.get(k) != current.get(k)
            for k in (
                "plan_digest",
                "source_environment_id",
                "target_environment_id",
                "workload_id",
            )
        ):
            raise ObservationError("security_scope_changed")
        now = self.clock()
        return {
            "kind": "console-security.v1",
            "plan_id": plan_id,
            "plan_digest": current["plan_digest"],
            "queried_at": now.isoformat(),
            "for_gate_decisions": False,
            "environments": [
                {
                    "environment_id": ref,
                    "side": side,
                    "cells": project_summary(body, env, int(now.timestamp() * 1000)),
                }
                for body, env, ref, side in zip(
                    bodies, names, refs, ("source", "target"), strict=True
                )
            ],
        }
