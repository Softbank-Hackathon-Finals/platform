"""Current plan-scoped public keys in T10's existing append-only command store.

Trusted host port only. No private keys, HTTP enrollment, signing or default
management grants. Every writer follows the existing per-plan transaction lock.
"""

from contextlib import contextmanager
from dataclasses import asdict, replace

from .contract import GateError, canonical, decode, fingerprint, server_time, time
from .models import identity
from .signatures import AuthorityKey

KEY_PREFIX = "t19-authority-key:"

PURPOSES = frozenset(
    {
        "context",
        "run-source",
        "run-target",
        "exception",
        "gate-result",
        "readiness-binding",
        "readiness-snapshot",
        "network-binding",
        "network-artifact",
    }
)

MAX_KEYS = 64

KEY_FIELDS = {"public_key_hex", "tenants", "purposes", "valid_from", "valid_until", "revoked"}

RECORD_FIELDS = {
    "kind",
    "plan_id",
    "tenant_id",
    "plan_digest",
    "issuer_id",
    "key_id",
    "producer_subject_id",
    "revision",
    "key",
    "enrolled_at",
    "enrolled_by",
    "revoked_at",
    "revoked_by",
}


def _exact(value, fields):
    if type(value) is not dict or set(value) != fields:
        raise GateError("AUTHORITY_RECORD_INVALID")


def _key_ref(issuer_id, key_id):
    identity(issuer_id)
    identity(key_id)
    return KEY_PREFIX + fingerprint([issuer_id, key_id]) + ":"


def _decode(raw):
    if type(raw) is not str or len(raw.encode()) > 32768:
        raise GateError("AUTHORITY_RECORD_INVALID")
    return decode(raw.encode())


def _key(value):
    _exact(value, KEY_FIELDS)
    if type(value["tenants"]) is not list or type(value["purposes"]) is not list:
        raise GateError("AUTHORITY_RECORD_INVALID")
    return AuthorityKey(
        **(value | {"tenants": tuple(value["tenants"]), "purposes": tuple(value["purposes"])})
    )


class PlanAuthorityKeys:
    """Explicit management port and current public-key provider on a plan view.

    gate:manage-provenance is separate from producer, record, read and evaluator
    grants. The host authenticates the principal before invoking this internal
    port. Enrollment and irreversible revocation never advance the plan state.
    """

    def __init__(self, records, *, clock):
        from bravo_platform_events.gate_records import GateRecords

        if not isinstance(records, GateRecords) or not callable(clock):
            raise GateError("EXPLICIT_SERVER_AUTHORITY_REQUIRED")
        self.records, self.clock = records, clock

    def _now(self):
        return server_time(self.clock)

    @contextmanager
    def _transaction(self, principal, plan_id, action):
        from bravo_platform_events.store import EventError

        try:
            with self.records.store._transaction(write=True, plan_id=plan_id) as c:
                started = self._now()
                plan = self.records._plan(c, principal, plan_id, action)
                yield c, plan
                if self.records._plan(c, principal, plan_id, action) != plan:
                    raise GateError("AUTHORITY_PLAN_CHANGED")
                if self._now() < started:
                    raise GateError("CLOCK_REGRESSION")
        except GateError:
            raise
        except EventError as error:
            raise GateError(
                "AUTHORITY_FORBIDDEN" if error.code == "forbidden" else "AUTHORITY_PLAN_CHANGED"
            ) from None
        except Exception:
            raise GateError("AUTHORITY_STORE_UNAVAILABLE") from None

    def _guard(self, view):
        from bravo_platform_events.gate_records import GateRecordView

        if type(view) is not GateRecordView or view.records is not self.records:
            raise GateError("SERVER_RECORD_VIEW_REQUIRED")
        try:
            view._check(view.principal.subject_id, view.context)
        except Exception:
            raise GateError("AUTHORITY_VIEW_UNAVAILABLE") from None

    def _head(self, c, plan, issuer_id, key_id):
        prefix = _key_ref(issuer_id, key_id)
        rows = c.execute(
            "SELECT command_key,receipt_json FROM plan_commands "
            "WHERE plan_id=? AND command_key LIKE ? ORDER BY command_key LIMIT 3",
            (plan["plan_id"], prefix + "%"),
        ).fetchall()
        if not rows:
            return None
        if len(rows) > 2:
            raise GateError("AUTHORITY_RECORD_INVALID")
        previous = None
        for revision, row in enumerate(rows, 1):
            value = _decode(row["receipt_json"])
            _exact(value, RECORD_FIELDS)
            key = _key(value["key"])
            if (
                row["command_key"] != prefix + str(revision)
                or value["kind"] != "t19-authority-key.v1"
                or value["plan_id"] != plan["plan_id"]
                or value["tenant_id"] != plan["tenant_id"]
                or value["plan_digest"] != decode(plan["spec_json"].encode())["plan_digest"]
                or (value["issuer_id"], value["key_id"]) != (issuer_id, key_id)
                or type(value["revision"]) is not int
                or value["revision"] != revision
                or key.tenants != (plan["tenant_id"],)
                or len(key.purposes) != 1
                or not set(key.purposes) <= PURPOSES
            ):
                raise GateError("AUTHORITY_RECORD_INVALID")
            for name in ("producer_subject_id", "enrolled_by"):
                identity(value[name])
            time(value["enrolled_at"])
            if value["producer_subject_id"] == value["enrolled_by"] or time(
                value["enrolled_at"]
            ) >= time(key.valid_until):
                raise GateError("AUTHORITY_RECORD_INVALID")
            if revision == 1:
                if (
                    key.revoked
                    or value["revoked_at"] is not None
                    or value["revoked_by"] is not None
                ):
                    raise GateError("AUTHORITY_RECORD_INVALID")
            else:
                identity(value["revoked_by"])
                if (
                    not key.revoked
                    or time(value["revoked_at"]) < time(value["enrolled_at"])
                    or value
                    != (
                        previous
                        | {
                            "revision": 2,
                            "key": previous["key"] | {"revoked": True},
                            "revoked_at": value["revoked_at"],
                            "revoked_by": value["revoked_by"],
                        }
                    )
                ):
                    raise GateError("AUTHORITY_RECORD_INVALID")
            previous = value
        return previous

    @staticmethod
    def _insert(c, plan_id, ref, value):
        raw = canonical(value)
        if len(raw) > 32768:
            raise GateError("AUTHORITY_RECORD_INVALID")
        c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)", (plan_id, ref, "{}", raw.decode()))

    def enroll(
        self, principal, plan_id, *, issuer_id, key_id, producer_subject_id, key, expected_revision
    ):
        identity(producer_subject_id)
        prefix = _key_ref(issuer_id, key_id)
        if (
            type(key) is not AuthorityKey
            or key.revoked
            or len(key.purposes) != 1
            or type(expected_revision) is not int
            or expected_revision != 0
            or not set(key.purposes) <= PURPOSES
        ):
            raise GateError("AUTHORITY_ENROLLMENT_INVALID")
        with self._transaction(principal, plan_id, "gate:manage-provenance") as (c, plan):
            at = self._now()
            if (
                producer_subject_id == principal.subject_id
                or key.tenants != (plan["tenant_id"],)
                or time(key.valid_until) <= at
            ):
                raise GateError("AUTHORITY_ENROLLMENT_INVALID")
            raw_key = decode(canonical(asdict(key)))
            old = self._head(c, plan, issuer_id, key_id)
            if old is not None:
                if (
                    old["revision"] != 1
                    or old["key"] != raw_key
                    or old["producer_subject_id"] != producer_subject_id
                    or old["enrolled_by"] != principal.subject_id
                ):
                    raise GateError("AUTHORITY_KEY_CONFLICT")
                return old
            rows = c.execute(
                "SELECT command_key,receipt_json FROM plan_commands WHERE plan_id=? "
                "AND command_key LIKE ? LIMIT ?",
                (plan_id, KEY_PREFIX + "%", MAX_KEYS * 2 + 1),
            ).fetchall()
            keys = [_decode(row["receipt_json"]) for row in rows]
            if len(keys) >= MAX_KEYS * 2:
                raise GateError("AUTHORITY_RECORD_BUDGET")
            checked = set()
            for row, value in zip(rows, keys, strict=True):
                _exact(value, RECORD_FIELDS)
                pair = (value["issuer_id"], value["key_id"])
                if type(value["revision"]) is not int or row["command_key"] != _key_ref(
                    *pair
                ) + str(value["revision"]):
                    raise GateError("AUTHORITY_RECORD_INVALID")
                if pair not in checked:
                    if self._head(c, plan, *pair) is None:
                        raise GateError("AUTHORITY_RECORD_INVALID")
                    checked.add(pair)
            if sum(v.get("revision") == 1 for v in keys) >= MAX_KEYS or any(
                v.get("key", {}).get("public_key_hex") == key.public_key_hex for v in keys
            ):
                raise GateError("AUTHORITY_KEY_REUSE_OR_BUDGET")
            value = dict(
                kind="t19-authority-key.v1",
                plan_id=plan_id,
                tenant_id=plan["tenant_id"],
                plan_digest=decode(plan["spec_json"].encode())["plan_digest"],
                issuer_id=issuer_id,
                key_id=key_id,
                producer_subject_id=producer_subject_id,
                revision=1,
                key=raw_key,
                enrolled_at=at.isoformat(),
                enrolled_by=principal.subject_id,
                revoked_at=None,
                revoked_by=None,
            )
            self._insert(c, plan_id, prefix + "1", value)
            return value

    def revoke(self, principal, plan_id, *, issuer_id, key_id, expected_revision):
        if type(expected_revision) is not int or expected_revision not in (1, 2):
            raise GateError("AUTHORITY_REVISION_CONFLICT")
        with self._transaction(principal, plan_id, "gate:manage-provenance") as (c, plan):
            current = self._head(c, plan, issuer_id, key_id)
            if current is None or (current["revision"] == 1 and expected_revision != 1):
                raise GateError("AUTHORITY_REVISION_CONFLICT")
            if current["revision"] == 2:
                return current
            at = self._now()
            if at < time(current["enrolled_at"]):
                raise GateError("CLOCK_REGRESSION")
            value = current | {
                "revision": 2,
                "key": current["key"] | {"revoked": True},
                "revoked_at": at.isoformat(),
                "revoked_by": principal.subject_id,
            }
            self._insert(c, plan_id, _key_ref(issuer_id, key_id) + "2", value)
            return value

    @staticmethod
    def _effective_key(record):
        if record is None:
            return None
        key = _key(record["key"])
        return replace(
            key, valid_from=max(time(key.valid_from), time(record["enrolled_at"])).isoformat()
        )

    def read_key(self, view, issuer_id, key_id):
        """Current trust material, not authority by its mere presence."""
        try:
            self._guard(view)
            result = self._effective_key(self._head(view.c, view.plan, issuer_id, key_id))
            self._guard(view)
            return result
        except GateError:
            raise
        except Exception:
            raise GateError("AUTHORITY_STORE_UNAVAILABLE") from None
