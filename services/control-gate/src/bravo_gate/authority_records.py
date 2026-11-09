"""Plan-scoped public signature publication using current registered key records.

No private keys, signing service or producer qualification is supplied here.
The host authenticates producers and external T13/exception/network subjects.
"""

from dataclasses import asdict, dataclass

from .contract import GateError, fingerprint, invoke, time
from .key_records import PURPOSES, PlanAuthorityKeys, _decode, _exact
from .models import digest
from .signatures import AuthorityEnvelope, Ed25519Authority

ENVELOPE_PREFIX = "t19-authority-envelope:"

MAX_ENVELOPES = 4096

ENVELOPE_FIELDS = {
    "kind",
    "context_digest",
    "subject_digest",
    "purpose",
    "envelope",
    "published_at",
    "published_by",
}


def _envelope_ref(context, subject, purpose):
    digest(subject)
    if purpose not in PURPOSES:
        raise GateError("AUTHORITY_PURPOSE_UNSUPPORTED")
    return ENVELOPE_PREFIX + fingerprint([context.fingerprint, subject, purpose])


@dataclass(frozen=True)
class AuthoritySubject:
    """Host-authenticated external subject metadata, never a client assertion.

    T13/exception/network records must be re-read under the supplied plan view.
    observed_at is the immutable source observation, not the time of this lookup.
    """

    context_digest: str
    subject_digest: str
    purpose: str
    observed_at: str

    def __post_init__(self):
        digest(self.context_digest)
        digest(self.subject_digest)
        if self.purpose not in PURPOSES:
            raise GateError("AUTHORITY_PURPOSE_UNSUPPORTED")
        time(self.observed_at)


class PlanAuthorityRecords(PlanAuthorityKeys):
    """Current signature callbacks for StoredGateServices, not a new server.

    Publication requires gate:publish-provenance and the exact enrolled producer
    plus current plan read access. It does not enroll keys or authenticate probes.
    """

    def __init__(self, records, *, clock, external_subject_provider=None):
        super().__init__(records, clock=clock)
        if external_subject_provider is not None and not callable(external_subject_provider):
            raise GateError("EXPLICIT_SERVER_AUTHORITY_REQUIRED")
        self.external = external_subject_provider

    def _subject(self, view, envelope, record):
        ctx = view.context
        purpose, subject = envelope.purpose, envelope.subject_digest
        if envelope.context_digest != ctx.fingerprint:
            raise GateError("SIGNATURE_BINDING_MISMATCH")
        start = finish = None
        if purpose == "context":
            if subject != ctx.fingerprint:
                raise GateError("SIGNATURE_BINDING_MISMATCH")
        elif purpose in ("run-source", "run-target"):
            snapshot = view.run_provider(
                view.principal.subject_id, ctx, purpose.removeprefix("run-")
            )
            if snapshot.fingerprint != subject:
                raise GateError("SIGNATURE_BINDING_MISMATCH")
            start, finish = (time(snapshot.document[k]) for k in ("started_at", "finished_at"))
        elif purpose == "gate-result":
            snapshot = view.report_provider(view.principal.subject_id, ctx)
            if snapshot is None or snapshot.fingerprint != subject:
                raise GateError("SIGNATURE_BINDING_MISMATCH")
            start = finish = time(snapshot.document["evaluated_at"])
        else:
            if self.external is None:
                raise GateError("EXTERNAL_AUTHORITY_SUBJECT_REQUIRED")
            value = invoke(self.external, view, purpose, subject)
            if (
                type(value) is not AuthoritySubject
                or value.context_digest != ctx.fingerprint
                or value.subject_digest != subject
                or value.purpose != purpose
            ):
                raise GateError("AUTHORITY_SUBJECT_MISMATCH")
            start = finish = time(value.observed_at)
        trusted_since = max(time(record["enrolled_at"]), time(record["key"]["valid_from"]))
        if start is not None and not trusted_since <= start <= finish <= time(envelope.issued_at):
            raise GateError("SUBJECT_PREDATES_AUTHORITY_ENROLLMENT")

    def publish(self, principal, plan_id, envelope):
        if type(envelope) is not AuthorityEnvelope:
            raise GateError("EXPLICIT_SIGNATURE_ENVELOPE_REQUIRED")
        with self._transaction(principal, plan_id, "gate:publish-provenance") as (c, plan):
            view = self.records.view(c, principal, plan)
            self._guard(view)
            ref = _envelope_ref(view.context, envelope.subject_digest, envelope.purpose)
            record = self._head(c, plan, envelope.issuer_id, envelope.key_id)
            if record is None or record["producer_subject_id"] != principal.subject_id:
                raise GateError("AUTHORITY_PRODUCER_MISMATCH")
            self._subject(view, envelope, record)
            authority = Ed25519Authority(
                envelope_provider=lambda *_: envelope,
                key_provider=lambda issuer, key: self._effective_key(
                    self._head(c, plan, issuer, key)
                ),
                clock=self._now,
            )
            authority.verify(view.context, envelope.subject_digest, envelope.purpose)
            self._guard(view)
            old = self._load_envelope(view, envelope.subject_digest, envelope.purpose)
            payload = asdict(envelope)
            if old:
                if old["envelope"] != payload or old["published_by"] != principal.subject_id:
                    raise GateError("AUTHORITY_ENVELOPE_CONFLICT")
                authority.verify(view.context, envelope.subject_digest, envelope.purpose)
                return old
            count = c.execute(
                "SELECT count(*) AS n FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                (plan_id, ENVELOPE_PREFIX + "%"),
            ).fetchone()["n"]
            if count >= MAX_ENVELOPES:
                raise GateError("AUTHORITY_RECORD_BUDGET")
            published_at = self._now()
            if published_at < time(envelope.issued_at):
                raise GateError("CLOCK_REGRESSION")
            value = dict(
                kind="t19-authority-envelope.v1",
                context_digest=view.context.fingerprint,
                subject_digest=envelope.subject_digest,
                purpose=envelope.purpose,
                envelope=payload,
                published_at=published_at.isoformat(),
                published_by=principal.subject_id,
            )
            self._insert(c, plan_id, ref, value)
            authority.verify(view.context, envelope.subject_digest, envelope.purpose)
            self._guard(view)
            return value

    def _load_envelope(self, view, subject, purpose):
        self._guard(view)
        ctx = view.context
        row = view.c.execute(
            "SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key=?",
            (view.plan["plan_id"], _envelope_ref(ctx, subject, purpose)),
        ).fetchone()
        if row is None:
            return None
        record = _decode(row["receipt_json"])
        _exact(record, ENVELOPE_FIELDS)
        _exact(record["envelope"], set(AuthorityEnvelope.__dataclass_fields__))
        envelope = AuthorityEnvelope(**record["envelope"])
        enrolled = self._head(view.c, view.plan, envelope.issuer_id, envelope.key_id)
        if (
            record["kind"] != "t19-authority-envelope.v1"
            or enrolled is None
            or record["published_by"] != enrolled["producer_subject_id"]
            or record["context_digest"] != ctx.fingerprint
            or record["subject_digest"] != subject
            or record["purpose"] != purpose
            or envelope.subject_digest != subject
            or envelope.purpose != purpose
            or not time(envelope.issued_at) <= time(record["published_at"]) <= self._now()
        ):
            raise GateError("AUTHORITY_RECORD_INVALID")
        self._subject(view, envelope, enrolled)
        self._guard(view)
        return record

    def authority(self, view):
        """Ed25519 callbacks re-read enrollment/revocation on this exact connection."""
        self._guard(view)

        def key_provider(issuer, key_id):
            return self.read_key(view, issuer, key_id)

        def envelope_provider(ctx, subject, purpose):
            self._guard(view)
            if ctx != view.context:
                raise GateError("SIGNATURE_BINDING_MISMATCH")
            record = self._load_envelope(view, subject, purpose)
            if record is None:
                return None
            return AuthorityEnvelope(**record["envelope"])

        return Ed25519Authority(
            envelope_provider=envelope_provider, key_provider=key_provider, clock=self._now
        )
