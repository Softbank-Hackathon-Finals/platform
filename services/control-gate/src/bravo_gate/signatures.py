"""Current scoped Ed25519 authority; signature proves provenance, not a probe.

Keys/envelopes come from T10's registered stores. Private keys are never accepted
by the evaluator. Revoked/rotated keys and changed envelopes fail closed.
"""

from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contract import GateError, canonical, invoke, server_time, time
from .models import VerifiedAuthority, digest, identity

DOMAIN = b"BRAVO-T19-AUTHORITY-V1\x00"


@dataclass(frozen=True)
class AuthorityEnvelope:
    issuer_id: str
    key_id: str
    purpose: str
    context_digest: str
    subject_digest: str
    issued_at: str
    expires_at: str
    signature_hex: str

    def __post_init__(self):
        for value in (self.issuer_id, self.key_id, self.purpose):
            identity(value)
        digest(self.context_digest)
        digest(self.subject_digest)
        if (
            type(self.signature_hex) is not str
            or len(self.signature_hex) != 128
            or any(c not in "0123456789abcdef" for c in self.signature_hex)
            or not time(self.issued_at) < time(self.expires_at)
        ):
            raise GateError("INVALID_SIGNATURE_ENVELOPE")

    @property
    def payload(self):
        return {k: v for k, v in vars(self).items() if k != "signature_hex"}


@dataclass(frozen=True)
class AuthorityKey:
    public_key_hex: str
    tenants: tuple[str, ...]
    purposes: tuple[str, ...]
    valid_from: str
    valid_until: str
    revoked: bool

    def __post_init__(self):
        digest(self.public_key_hex)
        for values in (self.tenants, self.purposes):
            if (
                type(values) is not tuple
                or not 1 <= len(values) <= 32
                or len(set(values)) != len(values)
            ):
                raise GateError("EXPLICIT_KEY_SCOPE_REQUIRED")
            for value in values:
                identity(value)
        if type(self.revoked) is not bool or not time(self.valid_from) < time(self.valid_until):
            raise GateError("INVALID_KEY_WINDOW")


class Ed25519Authority:
    def __init__(self, *, envelope_provider, key_provider, clock):
        if not all(callable(v) for v in (envelope_provider, key_provider, clock)):
            raise GateError("EXPLICIT_SERVER_AUTHORITY_REQUIRED")
        self.envelope_provider = envelope_provider
        self.key_provider = key_provider
        self.clock = lambda: server_time(clock)

    def verify(self, ctx, subject_digest, purpose):
        envelope = invoke(self.envelope_provider, ctx, subject_digest, purpose)
        if (
            not isinstance(envelope, AuthorityEnvelope)
            or envelope.context_digest != ctx.fingerprint
            or envelope.subject_digest != subject_digest
            or envelope.purpose != purpose
        ):
            raise GateError("SIGNATURE_BINDING_MISMATCH")
        key = invoke(self.key_provider, envelope.issuer_id, envelope.key_id)

        def verify_current():
            now = self.clock()
            if (
                not isinstance(key, AuthorityKey)
                or type(key.revoked) is not bool
                or key.revoked
                or ctx.tenant_id not in key.tenants
                or purpose not in key.purposes
                or not time(key.valid_from)
                <= time(envelope.issued_at)
                <= now
                < min(time(key.valid_until), time(envelope.expires_at))
            ):
                raise GateError("SIGNING_AUTHORITY_REVOKED_OR_EXPIRED")
            try:
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(key.public_key_hex)).verify(
                    bytes.fromhex(envelope.signature_hex), DOMAIN + canonical(envelope.payload)
                )
            except (ValueError, InvalidSignature):
                raise GateError("SIGNATURE_INVALID") from None

        verify_current()
        if (
            invoke(self.key_provider, envelope.issuer_id, envelope.key_id) != key
            or invoke(self.envelope_provider, ctx, subject_digest, purpose) != envelope
        ):
            raise GateError("SIGNING_AUTHORITY_CHANGED")
        verify_current()
        return VerifiedAuthority(
            ctx.fingerprint,
            subject_digest,
            purpose,
            min(time(key.valid_until), time(envelope.expires_at)),
        )
