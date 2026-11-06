"""CloudWatch audit observation tied to a verified temporary reader identity.

Authenticated host owns registration, effect receipt, credentials and retention.
No external report upload, role issuance or environment mutation is provided.
"""

import hashlib
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .audit_probes import LOG_ARN, TESTS, AuditSearchProbe, CanaryReceipt, CloudWatchAuditSearch
from .aws_identity import IdentityUnavailable, RegisteredRole, verified_client
from .runner import Environment, RunContext, TestSpec


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise IdentityUnavailable('AWS_AUDIT_TIME_INVALID')
    return value.astimezone(UTC)


def _sha(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


class QualifiedCloudWatchAuditProbe:
    def __init__(self, *, app, environment, reader_role, log_group_arn, streams,
                 credential_provider, receipt_provider, evidence_sink,
                 clock=lambda: datetime.now(UTC), monotonic=time.monotonic):
        match = LOG_ARN.fullmatch(log_group_arn) if isinstance(log_group_arn, str) else None
        if (not isinstance(app, str) or not 1 <= len(app) <= 128
                or not isinstance(environment, Environment) or not isinstance(reader_role, RegisteredRole)
                or not match or (reader_role.arn.split(':')[1], reader_role.arn.split(':')[4]) != (match[1], match[3])
                or not isinstance(streams, tuple) or not 1 <= len(streams) <= 100
                or len(set(streams)) != len(streams)
                or any(not isinstance(s, str) or not 1 <= len(s) <= 512
                       or any(ord(c) < 32 or c in ':*' for c in s) for s in streams)
                or not all(callable(f) for f in (credential_provider, receipt_provider, evidence_sink, clock, monotonic))):
            raise IdentityUnavailable('AWS_AUDIT_REGISTRATION_REQUIRED')
        self.app, self.environment, self.role = app, environment, reader_role
        self.arn, self.streams, self.region = log_group_arn, streams, match[2]
        self.provider, self.receipts, self.sink = credential_provider, receipt_provider, evidence_sink
        self.clock, self.monotonic = clock, monotonic

    def __call__(self, context, spec):
        if (not isinstance(context, RunContext) or context.app != self.app
                or context.environment != self.environment or not isinstance(spec, TestSpec)
                or spec.test_id not in TESTS
                or spec != TestSpec(spec.test_id, 'AUD-01', '1', 'present', 'not_applicable')):
            raise IdentityUnavailable('AWS_AUDIT_CONTEXT_MISMATCH')
        before, began = _utc(self.clock()), self.monotonic()
        try:
            receipt = self.receipts(context, spec)
        except Exception:
            raise IdentityUnavailable('AWS_AUDIT_RECEIPT_UNAVAILABLE') from None
        if (not isinstance(receipt, CanaryReceipt) or receipt.context != context
                or receipt.test_id != spec.test_id or receipt.completed_at > before
                or before - receipt.deadline > timedelta(minutes=10)):
            raise IdentityUnavailable('AWS_AUDIT_RECEIPT_MISMATCH')
        try:
            credentials = self.provider(self.role)
        except Exception:
            raise IdentityUnavailable('AWS_AUDIT_CREDENTIALS_UNAVAILABLE') from None
        retained = []

        def capture(raw):
            if type(raw) is not bytes or not 0 < len(raw) <= 2*1024*1024 or retained:
                raise IdentityUnavailable('AWS_AUDIT_EVIDENCE_INVALID')
            retained.append(raw)

        def check_budget():
            if not 0 <= self.monotonic() - began < 60:
                raise IdentityUnavailable('AWS_AUDIT_DEADLINE_EXCEEDED')

        check_budget()
        with verified_client(self.role, credentials, 'logs', self.region, clock=self.clock) as (client, identity):
            check_budget()
            search = CloudWatchAuditSearch(client, log_group_arn=self.arn, streams=self.streams,
                                           clock=self.clock, monotonic=self.monotonic)
            probe = AuditSearchProbe(receipt_provider=lambda ctx, method: receipt,
                                     search=search, source_binding=self.arn, streams=self.streams,
                                     evidence_sink=capture, clock=self.clock)
            observation = probe(context, spec)
            ended = _utc(self.clock())
            check_budget()
            if (not before <= observation.collected_at <= ended
                    or datetime.fromisoformat(identity['expires_at']) <= ended):
                raise IdentityUnavailable('AWS_AUDIT_EXPIRED_OR_CLOCK_CHANGED')
            if len(retained) != 1 or _sha(retained[0]) != observation.output_sha256:
                raise IdentityUnavailable('AWS_AUDIT_EVIDENCE_MISMATCH')
            envelope = json.dumps({
                'kind': 'qualified-cloudwatch-audit.v1', 'run_id': context.run_id,
                'app': context.app, 'environment': {'id': context.environment.id,
                    'role': context.environment.role, 'ref': context.environment.ref},
                'test_id': spec.test_id, 'reader_identity': identity,
                'source_sha256': _sha(self.arn.encode()),
                'streams_sha256': _sha(json.dumps(self.streams, separators=(',', ':')).encode()),
                'probe_sha256': observation.output_sha256, 'probe': json.loads(retained[0]),
                'completed_at': ended.isoformat(),
            }, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            try:
                self.sink(envelope)
            except Exception:
                raise IdentityUnavailable('AWS_AUDIT_RETENTION_FAILED') from None
            # Retention completion is part of qualification, not a post-PASS task.
            check_budget()
            final = _utc(self.clock())
            if final < ended or datetime.fromisoformat(identity['expires_at']) <= final:
                raise IdentityUnavailable('AWS_AUDIT_EXPIRED_OR_CLOCK_CHANGED')
            return replace(observation, tool='bravo-cloudwatch-audit-qualified', tool_version='1',
                           output_sha256=_sha(envelope))
