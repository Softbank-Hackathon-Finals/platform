"""Plan-bound IaC UI capabilities and authenticated customer GitOps observations.

No cluster credentials, API target selection, approval rules or reconciliation
are implemented here. Native T44/T10 APIs remain the only change authority.
"""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .observations import ObservationError, _plan
from .wire import object_document
from .workload import identifier, safe_text, stamp
from .workspace_data import call

SYNC_STATES = frozenset({
    "NOT_INSTALLED", "BOOTSTRAP_ONLY", "BOOTSTRAP_PENDING", "SYNC_PAUSED",
    "SYNC_IN_PROGRESS", "SYNC_RETRYING", "SYNC_PENDING", "BLOCKED", "SYNC_VERIFIED",
})


@dataclass(frozen=True, repr=False)
class CustomerTarget:
    environment_id: str
    environment: str
    cluster_uid: str
    reviewed_revision: str
    producer_revision: str
    provenance: str


@dataclass(frozen=True, repr=False)
class RegisteredCustomerSync:
    plan_id: str
    plan_digest: str
    plan_version: int
    environment_id: str
    cluster_uid: str
    source_revision: str
    content_sha256: str
    document: bytes


def customer_projection(value, target, plan, now):
    if (type(value) is not RegisteredCustomerSync or type(value.plan_version) is not int
            or (value.plan_id, value.plan_digest, value.plan_version, value.environment_id, value.cluster_uid)
            != (plan["plan_id"], plan["plan_digest"], plan["version"], target.environment_id, target.cluster_uid)
            or type(value.document) is not bytes or len(value.document) > 16384
            or hashlib.sha256(value.document).hexdigest() != value.content_sha256
            or value.source_revision != target.producer_revision):
        raise ValueError("invalid_customer_registration")
    doc = object_document(value.document, max_bytes=16384)
    required = {"environment", "expected_revision", "observed_at", "status", "reasons",
                "authorizes_cutover", "migration_verified", "control_equivalence_verified", "cluster_identity_matches"}
    if (set(doc) - required - {"retry_count", "synced_resource_count"} or not required <= set(doc)
            or doc["environment"] != target.environment or doc["expected_revision"] != target.reviewed_revision
            or doc["status"] not in SYNC_STATES or doc["cluster_identity_matches"] is not True
            or any(doc[key] is not False for key in ("authorizes_cutover", "migration_verified", "control_equivalence_verified"))
            or not timedelta(0) <= now - stamp(doc["observed_at"]) < timedelta(minutes=5)
            or type(doc["reasons"]) is not list or len(doc["reasons"]) > 20):
        raise ValueError("invalid_customer_observation")
    for reason in doc["reasons"]:
        if not isinstance(reason, str) or not reason or len(reason) > 80 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for c in reason):
            raise ValueError("invalid_customer_reason")
    for field in ("retry_count", "synced_resource_count"):
        if field in doc and (type(doc[field]) is not int or not 0 <= doc[field] <= 10**9):
            raise ValueError("invalid_customer_counter")
    if doc["status"] == "SYNC_VERIFIED" and (doc["reasons"] or doc.get("synced_resource_count", 0) < 1):
        raise ValueError("invalid_customer_success")
    if doc["status"] == "SYNC_RETRYING" and doc.get("retry_count", 0) < 1:
        raise ValueError("invalid_customer_retry")
    return dict(doc, environment_id=target.environment_id, source_revision=value.source_revision,
                provenance=target.provenance,
                content_digest=value.content_sha256,
                valid_until=(stamp(doc["observed_at"]) + timedelta(minutes=5)).isoformat())


class PlanDelivery:
    def __init__(self, api, *, authorize, subject, modules=(), targets=(),
                 load_sync=None, authenticate_sync=None, clock=None, serves=None):
        if not callable(authorize) or not callable(subject) or not (serves is None or callable(serves)):
            raise ValueError("delivery_host_authority_required")
        if load_sync is not None and not callable(authenticate_sync):
            raise ValueError("customer_producer_authentication_required")
        self.api, self.authorize, self.subject = api, authorize, subject
        self.modules = json.loads(json.dumps(modules))
        if len(self.modules) > 32:
            raise ValueError("delivery_registration_limit")
        for module in self.modules:
            if set(module) != {"environment_id", "environment", "module", "params"}:
                raise ValueError("invalid_iac_module_registration")
            for key in ("environment_id", "environment", "module"):
                identifier(module[key])
            if type(module["params"]) is not dict or len(json.dumps(module["params"])) > 8192:
                raise ValueError("invalid_iac_module_params")
            safe_text(json.dumps(module["params"]), 8192)
        if len({(m["environment_id"], m["module"]) for m in self.modules}) != len(self.modules):
            raise ValueError("duplicate_iac_module_registration")
        self.targets = tuple(targets)
        if len(self.targets) > 2 or len({t.environment_id for t in self.targets}) != len(self.targets):
            raise ValueError("invalid_customer_targets")
        for target in self.targets:
            if type(target) is not CustomerTarget:
                raise ValueError("invalid_customer_target")
            identifier(target.environment_id)
            safe_text(target.cluster_uid)
            if (target.environment not in {"source-dev", "target-dev"}
                    or target.provenance not in {"live", "synthetic", "historical"}
                    or any(type(v) is not str or len(v) != 40 or any(c not in "0123456789abcdef" for c in v)
                           for v in (target.reviewed_revision, target.producer_revision))):
                raise ValueError("invalid_customer_target")
        self.load_sync, self.authenticate_sync = load_sync, authenticate_sync
        # serves(scope, plan, target): whether a registered target belongs to this plan's tenant at all.
        # Environment IDs are unique only per tenant, so a target that does not is left out, not shown UNKNOWN.
        self.serves = serves
        self.clock = clock or (lambda: datetime.now(UTC))

    async def read(self, scope, plan_id):
        before = await _plan(self.api, scope, plan_id)
        if await call(self.authorize, scope, before, "read") is not True:
            raise ObservationError("forbidden", 403)
        actor = identifier(await call(self.subject, scope, before))
        pair = {before["source_environment_id"], before["target_environment_id"]}
        modules = [dict(m) for m in self.modules if m["environment_id"] in pair]
        native = any(getattr(r, "path", "") == "/v1/plans/{plan_id}/iac/plans" for r in getattr(self.api, "routes", ()))
        actions = {key: native and bool(modules) and await call(self.authorize, scope, before, key) is True
                   for key in ("apply_infra", "approve_infra")}
        customers = []
        verified = []
        for target in self.targets:
            if target.environment_id not in pair:
                continue
            if self.serves is not None and await call(self.serves, scope, before, target) is not True:
                continue
            result = dict(environment_id=target.environment_id, environment=target.environment,
                          expected_revision=target.reviewed_revision, provenance=target.provenance, status="UNCONFIGURED")
            if self.load_sync is not None:
                try:
                    value = await asyncio.wait_for(call(self.load_sync, scope, before, target), timeout=5)
                    if await call(self.authenticate_sync, scope, before, target, value) is not True:
                        raise ValueError("customer_producer_unverified")
                    result = customer_projection(value, target, before, self.clock())
                    if await call(self.authenticate_sync, scope, before, target, value) is not True:
                        raise ValueError("customer_producer_revoked")
                    verified.append((len(customers), target, value))
                except Exception:
                    result["status"] = "UNKNOWN"
            customers.append(result)
        after = await _plan(self.api, scope, plan_id)
        if before != after:
            raise ObservationError("delivery_plan_changed")
        if (await call(self.authorize, scope, after, "read") is not True
                or await call(self.subject, scope, after) != actor):
            raise ObservationError("forbidden", 403)
        # Permissions may have been revoked during collection. Recheck rather than cache grants.
        for key in actions:
            actions[key] = actions[key] and await call(self.authorize, scope, after, key) is True
        now = self.clock()
        for index, target, receipt in verified:
            if (now >= stamp(customers[index]["valid_until"])
                    or await call(self.authenticate_sync, scope, after, target, receipt) is not True):
                customers[index] = dict(environment_id=target.environment_id, environment=target.environment,
                                        expected_revision=target.reviewed_revision, provenance=target.provenance, status="UNKNOWN")
        return dict(kind="console-delivery.v1", plan_id=plan_id, plan_digest=after["plan_digest"],
                    plan_version=after["version"], subject_id=actor, queried_at=now.isoformat(),
                    expires_at=(now + timedelta(seconds=15)).isoformat(), authorizes_execution=False,
                    iac=dict(configured=native and bool(modules), modules=modules, actions=actions),
                    customers=customers)
