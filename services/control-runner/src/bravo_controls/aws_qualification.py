"""T14 AWS probes with actual temporary-session identity checks and retained evidence."""

import hashlib
import json
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime

from .aws_identity import (
    IdentityUnavailable,
    RegisteredRole,
    anonymous_s3_client,
    verified_client,
)
from .aws_probes import ObjectTarget, S3AnonymousReadProbe, SecretAccessProbe, SecretTarget
from .runner import RunContext, TestSpec


class QualifiedAwsProbe:
    """Registration and credential provider must come from the authenticated host.

The provider obtains a new bounded temporary credential snapshot per invocation.
It must not let the browser choose roles, accounts, resources, or the evidence sink.
This adapter issues no roles, creates no resources, and changes no policies.
"""

    def __init__(self, target, *, app_role, credential_provider, evidence_sink,
                 other_role=None, clock=lambda: datetime.now(UTC)):
        if (not isinstance(target, (SecretTarget, ObjectTarget))
                or not isinstance(app_role, RegisteredRole)
                or not all(callable(fn) for fn in (credential_provider, evidence_sink, clock))):
            raise IdentityUnavailable("AWS_QUALIFICATION_BINDING_REQUIRED")
        if isinstance(target, SecretTarget):
            if (not isinstance(other_role, RegisteredRole) or app_role.arn == other_role.arn
                    or app_role.role_id == other_role.role_id):
                raise IdentityUnavailable("DISTINCT_REGISTERED_ROLES_REQUIRED")
            self.service = "secretsmanager"
            self.region = target.secret_arn.split(":")[3]
            self.test_id = SecretAccessProbe.test_id
        else:
            if other_role is not None:
                raise IdentityUnavailable("ANONYMOUS_COMPARISON_REQUIRED")
            self.service = "s3"
            self.region = target.region
            self.test_id = S3AnonymousReadProbe.test_id
        account = target.secret_arn.split(":")[4] if isinstance(target, SecretTarget) else target.account_id
        partition = target.secret_arn.split(":")[1] if isinstance(target, SecretTarget) else target.partition
        for role in (app_role, other_role):
            if role and (role.arn.split(":")[1] != partition or role.arn.split(":")[4] != account):
                raise IdentityUnavailable("AWS_ROLE_TARGET_MISMATCH")
        self.target, self.app_role, self.other_role = target, app_role, other_role
        self.provider, self.sink, self.clock = credential_provider, evidence_sink, clock

    def __call__(self, context, spec):
        # Reject wrong scope before credentials are acquired or AWS is contacted.
        if (not isinstance(context, RunContext) or context.app != self.target.app
                or context.environment != self.target.environment
                or spec != TestSpec(self.test_id, self.test_id.rsplit(".", 1)[0], "1", "deny", "required")):
            raise IdentityUnavailable("AWS_QUALIFICATION_CONTEXT_MISMATCH")
        before = self.clock()
        identities, raw_evidence = [], []

        def retain_raw(value):
            if type(value) is not bytes or len(value) > 65536 or raw_evidence:
                raise IdentityUnavailable("AWS_PROBE_EVIDENCE_INVALID")
            raw_evidence.append(value)

        with ExitStack() as stack:
            def client(role):
                try:
                    credentials = self.provider(role)
                except Exception:
                    raise IdentityUnavailable("AWS_CREDENTIAL_PROVIDER_UNAVAILABLE") from None
                connection, identity = stack.enter_context(verified_client(
                    role, credentials, self.service, self.region, clock=self.clock))
                identities.append(identity)
                return connection

            app = client(self.app_role)
            common = {"app_client": app, "app_principal_arn": self.app_role.arn,
                      "evidence_sink": retain_raw, "clock": self.clock}
            if isinstance(self.target, SecretTarget):
                probe = SecretAccessProbe(self.target, other_client=client(self.other_role),
                                          other_principal_arn=self.other_role.arn, **common)
            else:
                anonymous = stack.enter_context(anonymous_s3_client(self.region))
                probe = S3AnonymousReadProbe(self.target, anonymous_client=anonymous, **common)
            observation = probe(context, spec)
            ended = self.clock()
            if (ended < before or ended < observation.collected_at
                    or any(datetime.fromisoformat(i["expires_at"]) <= ended for i in identities)):
                raise IdentityUnavailable("AWS_QUALIFICATION_EXPIRED_OR_CLOCK_CHANGED")
            if len(raw_evidence) != 1:
                raise IdentityUnavailable("AWS_PROBE_EVIDENCE_MISSING")
            encoded = raw_evidence[0]
            if observation.output_sha256 != "sha256:" + hashlib.sha256(encoded).hexdigest():
                raise IdentityUnavailable("AWS_PROBE_EVIDENCE_CHANGED")
            envelope = json.dumps({
                "kind": "qualified-aws-probe.v1", "run_id": context.run_id,
                "app": context.app, "environment_ref": context.environment.ref,
                "test_id": spec.test_id, "identities": identities,
                "probe": json.loads(encoded), "probe_sha256": observation.output_sha256,
            }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            try:
                self.sink(envelope)
            except Exception:
                raise IdentityUnavailable("AWS_QUALIFICATION_RETENTION_FAILED") from None
            return replace(observation, tool="bravo-controls-aws-qualified", tool_version="1",
                           output_sha256="sha256:" + hashlib.sha256(envelope).hexdigest())
