"""Current, plan-scoped product read grants in the existing T10 journal."""

import json

from .contract import validate_identifier
from .cutover import _get, _put
from .executor import _timestamp
from .product_inputs import _PRINCIPAL, PURPOSE, _identifier
from .store import EventError


class ProductInputAccess:
    """Trusted administration only; no HTTP grant or human permission conversion.

    An authenticated management decision must precede set_grant. Registration
    strings are references to that decision, not proof of operator authority.
    Empty/disabled grants revoke access. All writers share the plan lock.
    """

    def __init__(self, store, *, clock):
        if not callable(clock):
            raise ValueError("product_clock_required")
        self.store, self.clock = store, clock

    @staticmethod
    def _current(c, plan_id, principal):
        row = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ? "
                        "ORDER BY command_key DESC LIMIT 1",
                        (plan_id, f"product-grant:{principal}:%")).fetchone()
        return json.loads(row["receipt_json"]) if row else None

    def set_grant(self, plan_id, principal, *, tenant_id, enabled, valid_after, expires_at,
                  expected_revision, administration_ref, command_key):
        for item in (plan_id, tenant_id, administration_ref, command_key):
            validate_identifier(item)
        _identifier(principal, _PRINCIPAL)
        if (type(enabled) is not bool or type(expected_revision) is not int
                or not 0 <= expected_revision < 10**12
                or _timestamp(expires_at) <= _timestamp(valid_after)):
            raise ValueError("invalid_product_grant")
        request = dict(principal=principal, tenant_id=tenant_id, purpose=PURPOSE, enabled=enabled,
            valid_after=valid_after, expires_at=expires_at, expected_revision=expected_revision,
            administration_ref=administration_ref)
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            plan = c.execute("SELECT tenant_id FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None or plan["tenant_id"] != tenant_id:
                raise EventError("forbidden")
            key = "product-grant-command:" + command_key
            old = _get(c, plan_id, key)
            if old:
                if old[0] != request:
                    raise EventError("command_key_conflict")
                return old[1]
            current = self._current(c, plan_id, principal)
            if (current["revision"] if current else 0) != expected_revision:
                raise EventError("version_conflict")
            receipt = request | dict(revision=expected_revision + 1, recorded_at=self.clock())
            _put(c, plan_id, f"product-grant:{principal}:{receipt['revision']:013}", request, receipt)
            _put(c, plan_id, key, request, receipt)
            return receipt

    def require(self, connection, principal, plan, admission, purpose):
        _identifier(principal, _PRINCIPAL)
        now = _timestamp(self.clock())
        grant = self._current(connection, plan["plan_id"], principal)
        if (purpose != PURPOSE or admission.plan_id != plan["plan_id"]
                or admission.tenant_id != plan["tenant_id"] or not grant or not grant["enabled"]
                or grant["principal"] != principal or grant["purpose"] != PURPOSE
                or grant["tenant_id"] != plan["tenant_id"]
                or not _timestamp(grant["valid_after"]) <= now < _timestamp(grant["expires_at"])):
            raise EventError("forbidden")
