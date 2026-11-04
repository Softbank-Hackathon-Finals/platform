"""T36 window storage inside the existing T10 plan transaction.

This is a host port, not an HTTP authentication service. The host verifies the
dedicated producer credential and constructs ProducerIdentity; never construct
it from SliWindow or console claims. Binding administration is trusted-host only.
All binding writers and plan eligibility changes must share the same plan lock.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bravo_observability.intake import RETENTION, IntakeRejected, accept

from .contract import validate_identifier
from .executor import _timestamp
from .operator_access import OperatorAccess
from .plans import _canonical
from .store import EventError


@dataclass(frozen=True)
class ProducerIdentity:
    """Authenticated host mapping; credential_id is an opaque ID, never a token."""

    subject_id: str
    tenant_id: str
    plan_id: str
    credential_id: str
    expires_at: str

    def __post_init__(self):
        for value in (self.subject_id, self.tenant_id, self.plan_id, self.credential_id):
            validate_identifier(value)
        _timestamp(self.expires_at)


def _key(kind, value):
    return f"window-{kind}-" + hashlib.sha256(value.encode()).hexdigest()


def _binding(c, plan_id, subject_id):
    prefix = _key("binding", subject_id) + "-"
    row = c.execute(
        "SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ? "
        "ORDER BY command_key DESC LIMIT 1", (plan_id, prefix + "%"),
    ).fetchone()
    return json.loads(row["receipt_json"]) if row else None


class _TransactionWindows:
    """Connection-bound T36 WindowStore. Caller already owns the plan lock.

    Never opens a second connection or commits independently of authorization.
    No transport or tenant-facing interface may expose this class directly.
    """

    def __init__(self, connection, plan_id):
        self.c, self.plan_id = connection, plan_id

    @staticmethod
    def _us(value):
        return (_timestamp(value) - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)

    def _require_plan(self, plan_id):
        if plan_id != self.plan_id:
            raise IntakeRejected("plan_not_accepting_windows")

    def insert_if_newer(self, plan_id, start, end, document):
        self._require_plan(plan_id)
        start_us, end_us = self._us(start), self._us(end)
        if self.c.execute("SELECT 1 FROM sli_windows WHERE plan_id=? AND start_us=?",
                          (plan_id, start_us)).fetchone():
            return "duplicate"
        latest = self.c.execute("SELECT MAX(end_us) AS latest FROM sli_windows WHERE plan_id=?",
                                (plan_id,)).fetchone()["latest"]
        if latest is not None and end_us <= latest:
            return "out_of_order"
        if latest is not None and start_us < latest:
            return "overlapping_window"
        self.c.execute("INSERT INTO sli_windows VALUES (?,?,?,?)",
                       (plan_id, start_us, end_us, _canonical(document)))
        self.c.execute("DELETE FROM sli_windows WHERE plan_id=? AND end_us < ?",
                       (plan_id, end_us - RETENTION // timedelta(microseconds=1)))
        return "stored"

    def latest(self, plan_id):
        self._require_plan(plan_id)
        row = self.c.execute(
            "SELECT document_json FROM sli_windows WHERE plan_id=? ORDER BY end_us DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        return json.loads(row["document_json"]) if row else None


class WindowIntake:
    def __init__(self, store, *, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())

    def set_binding(self, producer: ProducerIdentity, *, enabled: bool,
                    expected_revision: int, administration_ref: str, command_key: str):
        """Trusted administrative port, never an enrollment endpoint.

        Host must authenticate/authorize administration_ref before calling.
        Disable or credential rotation takes effect under the admission's lock.
        A retry returns its old receipt and never re-enables a later revocation.
        """
        if not isinstance(producer, ProducerIdentity):
            raise EventError("forbidden")
        for value in (administration_ref, command_key):
            validate_identifier(value)
        if type(enabled) is not bool or type(expected_revision) is not int or not 0 <= expected_revision < 10**12:
            raise ValueError("invalid_window_binding")
        request = dict(subject_id=producer.subject_id, tenant_id=producer.tenant_id,
                       credential_id=producer.credential_id, expires_at=producer.expires_at,
                       enabled=enabled, expected_revision=expected_revision,
                       administration_ref=administration_ref)
        key = _key("admin", command_key)
        with self.store._transaction(write=True, plan_id=producer.plan_id) as c:
            plan = c.execute("SELECT * FROM plans WHERE plan_id=?", (producer.plan_id,)).fetchone()
            if plan is None or plan["tenant_id"] != producer.tenant_id:
                raise EventError("forbidden")
            old = c.execute("SELECT * FROM plan_commands WHERE plan_id=? AND command_key=?",
                            (producer.plan_id, key)).fetchone()
            if old:
                if old["request_json"] != _canonical(request):
                    raise EventError("command_key_conflict")
                return json.loads(old["receipt_json"])
            previous = _binding(c, producer.plan_id, producer.subject_id)
            if (previous["revision"] if previous else 0) != expected_revision:
                raise EventError("version_conflict")
            now = self.clock()
            if enabled and _timestamp(producer.expires_at) <= _timestamp(now):
                raise ValueError("expired_window_binding")
            receipt = request | dict(revision=expected_revision + 1, recorded_at=now,
                                     plan_version=plan["version"], plan_state=plan["state"],
                                     plan_digest=json.loads(plan["spec_json"])["plan_digest"])
            c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                      (producer.plan_id, _key("binding", producer.subject_id) + f"-{receipt['revision']:013}",
                       _canonical(request), _canonical(receipt)))
            c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
                      (producer.plan_id, key, _canonical(request), _canonical(receipt)))
        return receipt

    def _authorized(self, c, producer, now):
        plan = c.execute("SELECT * FROM plans WHERE plan_id=?", (producer.plan_id,)).fetchone()
        binding = _binding(c, producer.plan_id, producer.subject_id)
        if (plan is None or binding is None or binding["enabled"] is not True
                or plan["tenant_id"] != producer.tenant_id
                or binding["tenant_id"] != producer.tenant_id
                or binding["credential_id"] != producer.credential_id
                or binding["plan_version"] != plan["version"]
                or binding["plan_state"] != plan["state"]
                or binding["plan_digest"] != json.loads(plan["spec_json"])["plan_digest"]
                or not _timestamp(binding["recorded_at"]) <= _timestamp(now) < _timestamp(binding["expires_at"])
                or _timestamp(now) >= _timestamp(producer.expires_at)):
            raise EventError("forbidden")
        return plan

    def receive(self, producer: ProducerIdentity, document: object):
        """Return only after commit; StorageUnavailable may mean commit is unknown.

        T36 owns schema/statistics/time validation. This host owns identity,
        current plan binding and the transaction. A retained old window still
        becomes STALE under T36 judgement; receiving it never means PASS.
        """
        if not isinstance(producer, ProducerIdentity):
            raise EventError("forbidden")
        with self.store._transaction(write=True, plan_id=producer.plan_id) as c:
            now = self.clock()  # After waiting for the plan lock, not at HTTP entry.
            self._authorized(c, producer, now)
            result = accept(_TransactionWindows(c, producer.plan_id), document, now=now,
                            is_active_plan=lambda plan_id: plan_id == producer.plan_id)
        return result

    def latest(self, principal, plan_id):
        """Authenticated read port; use the host's existing plan read authority.

        T17 can use latest_in_transaction after checking its current plan/stage,
        so that it does not read a different snapshot from the judgement context.
        """
        validate_identifier(plan_id)
        with self.store._transaction() as c:
            plan = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise EventError("forbidden")
            OperatorAccess(self.store, clock=self.clock).check_read(c, principal, dict(plan))
            return self.latest_in_transaction(c, plan_id)

    @staticmethod
    def latest_in_transaction(connection, plan_id):
        """Trusted T17 host port; caller checks authority and stage in this transaction."""
        validate_identifier(plan_id)
        return _TransactionWindows(connection, plan_id).latest(plan_id)
