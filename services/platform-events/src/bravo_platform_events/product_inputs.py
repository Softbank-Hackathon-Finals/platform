"""Private T10 host ports for immutable T38 inputs; no token or network route.

The deployment supplies an authenticated source collector and current principal
authorization. Neither an Admission object nor a reference grants permission.
All admission, catalog, registration and revocation checks share the plan lock.
"""

import re
from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import uuid4

from bravo_agent.contracts import digest
from bravo_agent.product_contracts import ProductInput, validate_product_input
from rehearsal_runner.admission import logical_task_digest
from rehearsal_runner.task_catalog import CatalogUnavailable

from .contract import validate_identifier
from .cutover import _get, _put
from .executor import _timestamp
from .rehearsal import Admission, admitted_attempt
from .store import EventError

PURPOSE = "t38-product-proposal"
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_PRINCIPAL = re.compile(r"[a-f0-9]{64}")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)")


def _identifier(value, pattern):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise EventError("product_input_unavailable")


def _admission(value):
    if (type(value) is not Admission or type(value.plan_version) is not int
            or value.plan_version < 1 or type(value.attempt) is not int or value.attempt != 1):
        raise EventError("product_admission_unavailable")
    for name in ("plan_id", "tenant_id", "task_id", "rehearsal_id"):
        validate_identifier(getattr(value, name))
    _identifier(value.source_sha, re.compile(r"[a-f0-9]{40}"))
    _identifier(value.task_digest, _PRINCIPAL)


def _validated(value):
    try:
        return validate_product_input(value)
    except Exception:
        raise EventError("product_input_invalid") from None


class ProductInputRecords:
    """Trusted host administration and authenticated broker read ports.

    source(c, plan, admission, task) returns exactly bundle/manifest/files from
    the registered, approved immutable source. It cannot be model or HTTP input.
    authorize(c, principal, plan, admission, purpose) must raise unless the STS
    principal digest has a current registration for this tenant/plan/purpose.
    It is not an OperatorPrincipal and receives no JWT or cloud credentials.

    There is deliberately no default source, grant, tenant, token supplier or
    HTTP endpoint. The production transport must authenticate before read().
    """

    def __init__(self, store, *, catalog, authorize, source=None, clock=None):
        if (not callable(getattr(catalog, "for_plan", None)) or (source is not None and not callable(source))
                or not callable(authorize)):
            raise ValueError("product_input_ports_required")
        self.store, self.catalog, self.source, self.authorize = store, catalog, source, authorize
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def _current(self, c, admission):
        _admission(admission)
        row = c.execute("SELECT * FROM plans WHERE plan_id=?", (admission.plan_id,)).fetchone()
        if (row is None or row["tenant_id"] != admission.tenant_id
                or row["version"] != admission.plan_version or row["state"] != "REHEARSE"
                or admitted_attempt(c, admission.plan_id, admission.rehearsal_id, 1) != admission):
            raise EventError("product_admission_unavailable")
        plan = dict(row)
        try:
            task = self.catalog.for_plan(c, plan, admission.task_id)
            if task.source_sha != admission.source_sha or logical_task_digest(task) != admission.task_digest:
                raise ValueError("task_changed")
            task = replace(task, run_id=admission.rehearsal_id)
        except (CatalogUnavailable, EventError, ValueError):
            raise EventError("product_source_unavailable") from None
        return plan, task

    def _produce(self, c, plan, admission, task):
        try:
            parts = self.source(c, plan, admission, task)
            if not isinstance(parts, dict) or set(parts) != {"bundle", "manifest", "files"}:
                raise ValueError("source_shape")
            value = validate_product_input(ProductInput(
                admission_digest=digest(asdict(admission)), attempt=admission.attempt, **parts))
            self._bound(value, admission)
            return value
        except Exception:
            raise EventError("product_source_unavailable") from None

    @staticmethod
    def _bound(value, admission):
        if (value.admission_digest != digest(asdict(admission)) or value.attempt != 1
                or value.bundle.run_id != admission.rehearsal_id
                or value.bundle.source_sha != admission.source_sha):
            raise EventError("product_input_mismatch")

    def _permission(self, c, principal, plan, admission):
        try:
            self.authorize(c, principal, plan, admission, PURPOSE)
        except (EventError, ValueError):
            raise EventError("product_input_unavailable") from None

    def product_input(self, admission, task):
        """The #291 producer callback. It binds source to the committed intent."""
        _admission(admission)
        with self.store._transaction(write=True, plan_id=admission.plan_id) as c:
            plan, current = self._current(c, admission)
            if task != current:
                raise EventError("product_input_mismatch")
            return self._produce(c, plan, admission, current)

    def register(self, admission, product_input, *, principal, expires_at):
        """Write once per Admission; identical replay returns the original ref.

        Use the result as the reference/digest part of #291's request_for port.
        Its distinct, authenticated OIDC supplier owns web_identity_token.
        """
        _admission(admission)
        _identifier(principal, _PRINCIPAL)
        _identifier(expires_at, _UTC)
        expires_at = _timestamp(expires_at).astimezone(UTC).isoformat()
        value = _validated(product_input)
        self._bound(value, admission)
        record = dict(admission=asdict(admission), product_input=value.model_dump(mode="json"),
                      input_digest=digest(value), principal=principal, purpose=PURPOSE, expires_at=expires_at)
        slot = f"product-admission:{admission.rehearsal_id}:1"
        with self.store._transaction(write=True, plan_id=admission.plan_id) as c:
            plan, task = self._current(c, admission)
            self._permission(c, principal, plan, admission)
            if digest(self._produce(c, plan, admission, task)) != record["input_digest"]:
                raise EventError("product_input_mismatch")
            old = _get(c, admission.plan_id, slot)
            if old:
                ref = old[1]["input_ref"]
                saved = _get(c, admission.plan_id, "product-input:" + ref)
                if saved is None or saved[1] != record:
                    raise EventError("product_input_conflict")
            else:
                ref = uuid4().hex
            # A slow source read must not carry an expired/revoked grant across commit.
            self._permission(c, principal, plan, admission)
            self._usable(c, admission.plan_id, ref, record)
            result = dict(input_ref=ref, input_digest=record["input_digest"])
            if not old:
                _put(c, admission.plan_id, "product-input:" + ref, {}, record)
                _put(c, admission.plan_id, slot, {}, result)
            return result

    def _usable(self, c, plan_id, ref, record):
        if (record["purpose"] != PURPOSE or _timestamp(record["expires_at"]) <= _timestamp(self.clock())
                or _get(c, plan_id, "product-revoked:" + ref)):
            raise EventError("product_input_unavailable")

    def _locate(self, ref):
        _identifier(ref, _REF)
        with self.store._transaction() as c:
            rows = c.execute("SELECT plan_id FROM plan_commands WHERE command_key=? LIMIT 2",
                             ("product-input:" + ref,)).fetchall()
        if len(rows) != 1:
            raise EventError("product_input_unavailable")
        return rows[0]["plan_id"]

    def read(self, principal, input_ref):
        """Return six fields to T38 after current authority checks, without tokens."""
        _identifier(principal, _PRINCIPAL)
        plan_id = self._locate(input_ref)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            saved = _get(c, plan_id, "product-input:" + input_ref)
            if saved is None or saved[1]["principal"] != principal:
                raise EventError("product_input_unavailable")
            record = saved[1]
            admission = Admission(**record["admission"])
            if admission.plan_id != plan_id:
                raise EventError("product_input_unavailable")
            plan, _ = self._current(c, admission)
            self._permission(c, principal, plan, admission)
            value = _validated(record["product_input"])
            self._bound(value, admission)
            if digest(value) != record["input_digest"]:
                raise EventError("product_input_mismatch")
            self._permission(c, principal, plan, admission)
            self._usable(c, plan_id, input_ref, record)
            return record

    def revoke(self, input_ref, *, administration_ref):
        """Trusted management port only; durable and idempotent, never dispatches."""
        validate_identifier(administration_ref)
        plan_id = self._locate(input_ref)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            key = "product-revoked:" + input_ref
            old = _get(c, plan_id, key)
            if old:
                return old[1]
            receipt = dict(administration_ref=administration_ref, revoked_at=self.clock())
            _put(c, plan_id, key, {}, receipt)
            return receipt
