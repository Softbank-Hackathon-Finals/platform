"""ISO-01.1: paired object/DB reads for the same two registered apps.

Trusted worker library only. It never issues credentials, grants permissions or
accepts user-supplied evidence. A successful Secret test is not an isolation test.
"""

import re
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import uuid4

from .aws_identity import RegisteredRole, verified_client
from .aws_probes import (
    ObjectTarget,
    _canonical,
    _digest,
    _failure,
    _http,
    _object_client,
    _object_principal,
    _object_read,
    _principal,
)
from .runner import Observation, RunContext, TestSpec, _utc

SPEC = TestSpec("ISO-01.1", "ISO-01", "1", "deny", "required")


@dataclass(frozen=True)
class AccessLeg:
    context: RunContext
    nonce: str
    binding: str
    positive: bool
    outcome: str | None
    reason: str | None
    collected_at: datetime
    details: dict


class S3IsolationReader:
    """Ordinary GetObject, two STS-verified roles, positive reads before/after.

    Expired credentials, missing/version-changed objects and non-AccessDenied
    errors cannot prove isolation. Only this registered current object is tested;
    wildcard bucket policies, all objects and writes are not qualified here.
    """

    def __init__(
        self,
        owner,
        other,
        *,
        owner_role,
        other_role,
        credentials,
        read_registration,
        clock=lambda: datetime.now(UTC),
    ):
        if (
            not isinstance(owner, ObjectTarget)
            or not isinstance(other, ObjectTarget)
            or owner.provider != "s3"
            or other.provider != "s3"
            or owner.app == other.app
            or owner.environment != other.environment
            or owner.bucket == other.bucket
            or (owner.account_id, owner.region, owner.partition)
            != (other.account_id, other.region, other.partition)
            or not isinstance(owner_role, RegisteredRole)
            or not isinstance(other_role, RegisteredRole)
            or owner_role.arn == other_role.arn
            or owner_role.role_id == other_role.role_id
            or not all(callable(x) for x in (credentials, read_registration, clock))
        ):
            raise ValueError("distinct registered apps, resources and roles required")
        for role in (owner_role, other_role):
            _principal(role.arn, owner.account_id, owner.partition)
        self.owner, self.other = owner, other
        self.owner_role, self.other_role = owner_role, other_role
        self.credentials, self.registration, self.clock = credentials, read_registration, clock
        self.app, self.other_app, self.environment = owner.app, other.app, owner.environment

    @property
    def binding(self):
        return _digest(
            _canonical(
                {
                    "owner": asdict(self.owner),
                    "other": asdict(self.other),
                    "roles": [asdict(self.owner_role), asdict(self.other_role)],
                }
            )
        )

    @staticmethod
    def _read(client, target):
        try:
            response = client.get_object(
                Bucket=target.bucket, Key=target.key, ExpectedBucketOwner=target.account_id
            )
        except Exception as error:
            result = _failure(error, secret=False)
            return result["outcome"], result["reason"]
        body = response.get("Body") if isinstance(response, dict) else None
        valid = (
            _http(response) == 200
            and response.get("VersionId") == target.version_id
            and callable(getattr(body, "close", None))
        )
        if callable(getattr(body, "close", None)):
            try:
                body.close()
            except Exception:
                valid = False
        return ("allow", None) if valid else (None, "UNEXPECTED_RESPONSE")

    def __call__(self, context, nonce):
        if context.app != self.app or context.environment != self.environment:
            raise ValueError("object scope mismatch")
        binding = self.binding
        if self.registration(context) != binding:
            raise ValueError("object registration unavailable")
        with ExitStack() as stack:
            owner, owner_identity = stack.enter_context(
                verified_client(
                    self.owner_role,
                    self.credentials(self.owner_role),
                    "s3",
                    self.owner.region,
                    clock=self.clock,
                )
            )
            other, other_identity = stack.enter_context(
                verified_client(
                    self.other_role,
                    self.credentials(self.other_role),
                    "s3",
                    self.other.region,
                    clock=self.clock,
                )
            )
            before = [self._read(owner, self.owner)[0], self._read(other, self.other)[0]]
            cross = (
                self._read(other, self.owner) if before == ["allow", "allow"] else (None, "NOT_RUN")
            )
            after = [self._read(owner, self.owner)[0], self._read(other, self.other)[0]]
        if self.registration(context) != binding:
            raise ValueError("object registration changed")
        positive = before == after == ["allow", "allow"]
        outcome, reason = cross if positive else (None, "POSITIVE_CONTROL_FAILED")
        return AccessLeg(
            context,
            nonce,
            binding,
            positive,
            outcome,
            reason,
            _utc(self.clock()),
            {
                "identity_sha256": _digest(_canonical([owner_identity, other_identity])),
                "positive_before": before,
                "positive_after": after,
                "cross": outcome,
            },
        )


class ClientObjectIsolationReader:
    """ISO-01.1 object leg for onprem SeaweedFS (``s3-compatible``) and gcp (``gcs``).

    Same positive-before / cross / positive-after method as ``S3IsolationReader``, but
    the server supplies two pre-bound clients: SeaweedFS static S3 identities, or two
    GKE Workload Identity principals (KSA → GSA). There is no STS here, so identity
    verification belongs to the server registration; labels are bound into evidence.
    """

    providers = ("s3-compatible", "gcs")

    def __init__(
        self,
        owner,
        other,
        *,
        owner_client,
        other_client,
        owner_principal,
        other_principal,
        read_registration,
        clock=lambda: datetime.now(UTC),
    ):
        if (
            not isinstance(owner, ObjectTarget)
            or not isinstance(other, ObjectTarget)
            or owner.provider not in self.providers
            or owner.provider != other.provider
            or owner.app == other.app
            or owner.environment != other.environment
            or owner.bucket == other.bucket
            or (owner.account_id, owner.endpoint_url) != (other.account_id, other.endpoint_url)
            or owner_client is other_client
            or owner_principal == other_principal
            or not all(callable(x) for x in (read_registration, clock))
        ):
            raise ValueError("distinct registered apps, resources and principals required")
        for principal in (owner_principal, other_principal):
            _object_principal(owner, principal)
        self.owner, self.other = owner, other
        self.owner_client, self.other_client = owner_client, other_client
        self.principals = (owner_principal, other_principal)
        self.registration, self.clock = read_registration, clock
        self.app, self.other_app, self.environment = owner.app, other.app, owner.environment
        self._validate()

    def _validate(self):
        _object_client(self.owner, self.owner_client)
        _object_client(self.other, self.other_client)

    @property
    def binding(self):
        return _digest(
            _canonical(
                {
                    "owner": self.owner.binding(),
                    "other": self.other.binding(),
                    "apps": [self.app, self.other_app],
                    "principals": list(self.principals),
                }
            )
        )

    def __call__(self, context, nonce):
        if context.app != self.app or context.environment != self.environment:
            raise ValueError("object scope mismatch")
        binding = self.binding
        if self.registration(context) != binding:
            raise ValueError("object registration unavailable")
        self._validate()

        def read(client, target):
            outcome, _, reason = _object_read(target, client)
            return outcome, reason

        before = [read(self.owner_client, self.owner)[0], read(self.other_client, self.other)[0]]
        cross = (
            read(self.other_client, self.owner) if before == ["allow", "allow"] else (None, "NOT_RUN")
        )
        after = [read(self.owner_client, self.owner)[0], read(self.other_client, self.other)[0]]
        if self.registration(context) != binding:
            raise ValueError("object registration changed")
        positive = before == after == ["allow", "allow"]
        outcome, reason = cross if positive else (None, "POSITIVE_CONTROL_FAILED")
        return AccessLeg(
            context,
            nonce,
            binding,
            positive,
            outcome,
            reason,
            _utc(self.clock()),
            {
                "provider": self.owner.provider,
                "identity_sha256": _digest(_canonical(list(self.principals))),
                "positive_before": before,
                "positive_after": after,
                "cross": outcome,
            },
        )


class AppIsolationProbe:
    """Both resource legs must deny; neither can be dropped from the result."""

    def __init__(
        self, *, object_reader, database_reader, evidence_sink, clock=lambda: datetime.now(UTC)
    ):
        if (
            object_reader.app != database_reader.app
            or object_reader.other_app != database_reader.other_app
            or object_reader.environment != database_reader.environment
            or not all(callable(x) for x in (object_reader, database_reader, evidence_sink, clock))
        ):
            raise ValueError("same registered app pair and environment required")
        self.objects, self.database, self.sink, self.clock = (
            object_reader,
            database_reader,
            evidence_sink,
            clock,
        )

    def __call__(self, context, spec):
        if (
            spec != SPEC
            or context.app != self.objects.app
            or context.environment != self.objects.environment
        ):
            raise ValueError("isolation scope mismatch")
        started, nonce = _utc(self.clock()), str(uuid4())
        legs = []
        for reader in (self.objects, self.database):
            binding = reader.binding
            if not isinstance(binding, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", binding):
                raise ValueError("resource binding required")
            leg = reader(context, nonce)
            now = _utc(self.clock())
            if (
                not isinstance(leg, AccessLeg)
                or leg.context != context
                or leg.nonce != nonce
                or leg.binding != binding
                or reader.binding != binding
                or type(leg.positive) is not bool
                or leg.outcome not in {"allow", "deny", None}
                or leg.reason
                not in {
                    None,
                    "POSITIVE_CONTROL_FAILED",
                    "TIMEOUT",
                    "TOOL_ERROR",
                    "UNEXPECTED_RESPONSE",
                }
                or not started <= _utc(leg.collected_at) <= now
                or (leg.outcome is None) != (leg.reason is not None)
            ):
                raise ValueError("isolation leg invalid")
            legs.append(leg)
        positive = all(x.positive for x in legs)
        outcomes = [x.outcome for x in legs]
        reason = next((x.reason for x in legs if x.reason), None)
        if not positive:
            outcome, reason = None, "POSITIVE_CONTROL_FAILED"
        elif "allow" in outcomes:
            outcome, reason = "allow", None
        elif outcomes == ["deny", "deny"]:
            outcome, reason = "deny", None
        else:
            outcome, reason = None, reason or "UNEXPECTED_RESPONSE"
        payload = _canonical(
            {
                "run_id": context.run_id,
                "app": context.app,
                "environment": asdict(context.environment),
                "test_id": spec.test_id,
                "legs": [
                    {
                        "kind": kind,
                        "binding": leg.binding,
                        "outcome": leg.outcome,
                        "reason": leg.reason,
                        "positive": leg.positive,
                        "details": leg.details,
                    }
                    for kind, leg in zip(("object", "database"), legs, strict=True)
                ],
                "observed": outcome,
                "unknown_reason": reason,
            }
        )
        self.sink(payload)
        return Observation(
            context,
            spec.test_id,
            spec.method_version,
            outcome,
            "pass" if positive else "fail",
            _utc(self.clock()),
            "bravo-app-isolation",
            "1",
            _digest(payload),
            reason,
        )
