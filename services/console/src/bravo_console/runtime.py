"""Deploy-owned console composition over the existing, single T10/T15 host.

No fixture, permission enrollment endpoint, alternate gate or execution service.
Customer observations come directly from the registered TLS Kubernetes reader.
"""

import asyncio
import hashlib
import json
import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import PurePosixPath

from starlette.requests import Request

from .composition import compose_console
from .delivery import CustomerTarget, PlanDelivery, RegisteredCustomerSync
from .reports import PlanControlReport
from .t10_reports import T10ControlReports
from .workload import ID

log = logging.getLogger(__name__)
FIXED_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def failure_reason(error):
    """A fixed reason for the log: the collector's own code, otherwise only the exception type."""
    code = error.args[0] if type(error).__name__ == "IntakeError" and len(error.args) == 1 else None
    return code if isinstance(code, str) and FIXED_CODE.fullmatch(code) else type(error).__name__


class NativeCustomerSource:
    """Bounded background observations; serving a cached value never renews its age.

    Environment IDs are unique only within a tenant, so each registration names its tenant and an
    observation is served only for a plan of that tenant whose environment pair includes it."""

    def __init__(self, registrations, *, inspect_customer, reader, producer_revision, tenant_of):
        required = {
            "tenant_id",
            "environment_id",
            "environment",
            "cluster_uid",
            "reviewed_revision",
            "kubeconfig",
        }
        if (
            type(registrations) is not list
            or len(registrations) > 8
            or not callable(inspect_customer)
            or not callable(reader)
            or not callable(tenant_of)
            or not re.fullmatch(r"[0-9a-f]{40}", producer_revision)
        ):
            raise ValueError("fixed_customer_registrations_required")
        seen = set()
        for registration in registrations:
            if type(registration) is not dict or set(registration) != required:
                raise ValueError("fixed_customer_registration_required")
            path = registration["kubeconfig"]
            if (
                not isinstance(path, str)
                or not isinstance(registration["tenant_id"], str)
                or not ID.match(registration["tenant_id"])
                or not PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts
                or registration["environment_id"] in seen
            ):
                raise ValueError("unique_customer_and_protected_kubeconfig_required")
            seen.add(registration["environment_id"])
        self.targets = tuple(
            CustomerTarget(
                r["environment_id"],
                r["environment"],
                r["cluster_uid"],
                r["reviewed_revision"],
                producer_revision,
                "live",
            )
            for r in registrations
        )
        self.inputs = tuple(registrations)
        self.tenants = {r["environment_id"]: r["tenant_id"] for r in registrations}
        self.tenant_of = tenant_of
        self.inspect_customer, self.reader = inspect_customer, reader
        self.producer_revision = producer_revision
        self.lock = threading.Lock()
        self.documents, self.issued = {}, {}

    def collect(self, target):
        registered = next(r for r in self.inputs if r["environment_id"] == target.environment_id)
        get = self.reader(registered["kubeconfig"])
        result = self.inspect_customer(
            get,
            expected_cluster_uid=target.cluster_uid,
            environment=target.environment,
            revision=target.reviewed_revision,
        )
        raw = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        with self.lock:
            self.documents[target.environment_id] = raw
            self.issued = {k: v for k, v in self.issued.items() if k[-1] != target.environment_id}

    async def poll(self):
        while True:
            for target in self.targets:
                try:
                    await asyncio.to_thread(self.collect, target)
                except Exception as error:
                    log.warning(
                        "customer_collection_failed environment_id=%s reason=%s",
                        target.environment_id,
                        failure_reason(error),
                    )
                    # Don't keep presenting success after a failed collection.
                    with self.lock:
                        self.documents.pop(target.environment_id, None)
                        self.issued = {
                            k: v for k, v in self.issued.items() if k[-1] != target.environment_id
                        }
            await asyncio.sleep(30)

    def belongs(self, plan, target):
        """This registration is in the plan's environment pair and tenant."""
        return target.environment_id in (
            plan["source_environment_id"],
            plan["target_environment_id"],
        ) and self.tenant_of(plan["plan_id"]) == self.tenants.get(target.environment_id)

    def load(self, scope, plan, target):
        key = (plan["plan_id"], plan["plan_digest"], plan["version"], target.environment_id)
        if not self.belongs(plan, target):
            raise ValueError("customer_registration_outside_plan")
        with self.lock:
            raw = self.documents.get(target.environment_id)
            if raw is None:
                raise ValueError("customer_collection_pending")
            if key in self.issued and self.issued[key].document == raw:
                return self.issued[key]
            item = RegisteredCustomerSync(
                *key[:3],
                target.environment_id,
                target.cluster_uid,
                self.producer_revision,
                hashlib.sha256(raw).hexdigest(),
                raw,
            )
            # Only this fixed collector's observation can authenticate; arbitrary
            # self-supplied JSON/hash is never accepted through an HTTP route.
            if len(self.issued) >= 128:
                self.issued.clear()
            self.issued[key] = item
            return item

    def authenticate(self, scope, plan, target, item):
        key = (plan["plan_id"], plan["plan_digest"], plan["version"], target.environment_id)
        if not self.belongs(plan, target):
            return False
        with self.lock:
            return (
                self.issued.get(key) is item
                and self.documents.get(target.environment_id) == item.document
            )


class OperatorSession:
    """Reuse the original verified operator proof and per-plan OperatorAccess."""

    def __init__(self, store, access, verifier):
        self.store, self.access, self.verifier = store, access, verifier

    def principal(self, scope):
        headers = Request(scope).headers.getlist("authorization")
        if len(headers) != 1:
            return None
        previous = scope.get("sova.verified-operator")
        if (
            previous
            and previous[0] == headers[0]
            and datetime.now(UTC).timestamp() < previous[1].expires_at
        ):
            return previous[1]
        actor = self.verifier(Request(scope))
        if actor is not None:
            scope["sova.verified-operator"] = (headers[0], actor)
        return actor

    def authorize(self, scope, snapshot, permission):
        from bravo_platform_events.plan_api import authorized_plan
        from bravo_platform_events.store import EventError

        try:
            with self.store._transaction() as c:
                plan = authorized_plan(c, self.principal(scope), snapshot["plan_id"], None)
                if self.store._snapshot(c, plan) != snapshot:
                    return False
                self.access.require(c, self.principal(scope), plan, permission)
            return True
        except (EventError, ValueError):
            return False

    def subject(self, scope, snapshot):
        actor = self.principal(scope)
        if actor is None:
            raise ValueError("operator_session_unavailable")
        return actor.subject_id


def assemble(host, store, verifier, config, customer):
    from contextlib import asynccontextmanager

    from bravo_platform_events.gate_services import gate_records, provenance

    if (
        type(config) is not dict
        or set(config) != {"version", "public_origin", "modules", "customers"}
        or type(config["version"]) is not int
        or config["version"] != 1
        or not isinstance(customer, NativeCustomerSource)
    ):
        raise ValueError("invalid_console_runtime_config")
    if config["modules"] != []:
        # IaC module registrations carry no tenant yet and environment IDs repeat across tenants, so a
        # multi-tenant host would show one tenant's modules and params on another's plan (#727 review B).
        raise ValueError("native_modules_need_tenant_scope")
    session = OperatorSession(store, host.access, verifier)
    records = gate_records(store, host.access)
    source = T10ControlReports(
        records,
        resolve_principal=session.principal,
        authority_factory=provenance(records).authority,
    )
    controls = PlanControlReport(host.app, load=source.load, authenticate=source.authenticate)
    delivery = PlanDelivery(
        host.app,
        authorize=session.authorize,
        subject=session.subject,
        modules=config["modules"],
        targets=customer.targets,
        load_sync=customer.load,
        authenticate_sync=customer.authenticate,
        serves=lambda scope, plan, target: customer.belongs(plan, target),
    )
    original = host.app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            poller = asyncio.create_task(customer.poll())
            try:
                yield
            finally:
                poller.cancel()
                try:
                    await poller
                except asyncio.CancelledError:
                    pass

    host.app.router.lifespan_context = lifespan
    return compose_console(
        host.app,
        store,
        public_origin=config["public_origin"],
        resolve_principal=lambda request: session.principal(request.scope),
        authorize_panel=lambda scope, plan, feature: session.authorize(scope, plan, "read"),
        authorize_operation=lambda scope, plan, action: False,
        controls=controls,
        delivery=delivery,
    )
