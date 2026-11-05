"""Bounded observations for a credential-injection verification job.

Probes run independently of the proposed application. They must not return
credentials, secret data, logs, or an approval decision.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

Observation = Literal["yes", "no", "unknown"]


class CredentialProbe(Protocol):
    def secret_exists(self) -> Observation:
        """Check fixture existence with the trusted verifier's own access."""
        ...

    def app_connects(self) -> Observation:
        """Check the candidate app's DB connection without returning credentials."""
        ...

    def other_account_can_read(self) -> Observation:
        """Check the specifically scoped other service account's access."""
        ...


@dataclass(frozen=True)
class CredentialObservations:
    secret_before: Observation
    app_db_connection: Observation
    other_account_secret_read: Observation
    secret_after: Observation


def _observation(value: object) -> Observation:
    if value == "yes":
        return "yes"
    if value == "no":
        return "no"
    return "unknown"


def observe_credential_delivery(probe: CredentialProbe) -> CredentialObservations:
    """Observe both positive controls around a negative access check.

    Missing fixtures, ambiguous results and errors cannot establish denial.
    The caller interprets these observations; they are not a verifier receipt.
    """
    try:
        before = _observation(probe.secret_exists())
        connection = _observation(probe.app_connects())
        access = _observation(probe.other_account_can_read())
        after = _observation(probe.secret_exists())
    except Exception:
        return CredentialObservations("unknown", "unknown", "unknown", "unknown")
    return CredentialObservations(before, connection, access, after)
