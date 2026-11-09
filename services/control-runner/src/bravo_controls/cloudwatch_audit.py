"""Registered CNPG → CloudWatch configuration and read-only destination preflight.

No deployment, role creation, retention changes, log upload or gate PASS here.
The operator authenticates the file source and supplies a dedicated workload role.
"""

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .audit_forwarding import AuditFileRegistration, _file_config
from .aws_probes import _client

_GROUP = re.compile(r"arn:aws:logs:([a-z]{2}-[a-z]+-\d):([0-9]{12}):log-group:([A-Za-z0-9_./#-]{1,512})\Z")
_KEY = re.compile(r"arn:aws:kms:([a-z]{2}-[a-z]+-\d):([0-9]{12}):key/([a-f0-9-]{36})\Z")
_RETENTION = {1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 2192, 2557, 2922, 3288, 3653}


@dataclass(frozen=True)
class CloudWatchAuditRegistration(AuditFileRegistration):
    log_group_arn: str
    kms_key_arn: str
    retention_days: int

    def __post_init__(self):
        super().__post_init__()
        group = _GROUP.fullmatch(self.log_group_arn) if isinstance(self.log_group_arn, str) else None
        key = _KEY.fullmatch(self.kms_key_arn) if isinstance(self.kms_key_arn, str) else None
        if not group or not key or group.groups()[:2] != key.groups()[:2]:
            raise ValueError("exact same-account/region log group and KMS key required")
        if type(self.retention_days) is not int or self.retention_days not in _RETENTION:
            raise ValueError("explicit supported retention required")
        if group[3].startswith("aws/"):
            raise ValueError("reserved log group name")

    @property
    def region(self):
        return _GROUP.fullmatch(self.log_group_arn)[1]

    @property
    def account(self):
        return _GROUP.fullmatch(self.log_group_arn)[2]

    @property
    def group(self):
        return _GROUP.fullmatch(self.log_group_arn)[3]

    @property
    def stream(self):
        # A replaced Pod never inherits an earlier Pod's registered stream.
        return f"cnpg/{self.namespace}/{self.pod}/{self.pod_uid}/postgres"


def cloudwatch_collector_config(binding: CloudWatchAuditRegistration) -> dict:
    if not isinstance(binding, CloudWatchAuditRegistration):
        raise ValueError("trusted CloudWatch registration required")
    config = _file_config(binding)
    config["exporters"] = {"awscloudwatchlogs/audit": {
        "region": binding.region, "log_group_name": binding.group,
        "log_stream_name": binding.stream, "raw_log": True,
        "max_event_payload_bytes": 1048576,
        # Creation/retention are IaC responsibilities. Missing destinations fail
        # under the dedicated IAM policy, not an exporter-owned auto-create group.
        "retry_on_failure": {"enabled": True, "initial_interval": "1s",
                             "max_interval": "30s", "max_elapsed_time": "0s"},
        "sending_queue": {"enabled": True, "storage": "file_storage/audit",
                          "queue_size": 2048, "num_consumers": 1},
    }}
    config["service"]["pipelines"]["logs/cnpg"]["exporters"] = ["awscloudwatchlogs/audit"]
    # Only the pre-existing CNPG allowlist reaches raw_log. The provider exporter
    # interprets _aws/log_group_name/log_stream_name as routing instructions;
    # never pass uploaded JSON or arbitrary resource attributes to it.
    return config


def writer_policy(binding: CloudWatchAuditRegistration) -> dict:
    """Proposal for a dedicated role; never installs or proves effective IAM."""
    if not isinstance(binding, CloudWatchAuditRegistration):
        raise ValueError("trusted CloudWatch registration required")
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": binding.log_group_arn + ":log-stream:" + binding.stream,
    }]}


def _checked(client, service, region):
    _client(client, service, region)
    # Caller-provided SDK instances cannot silently redirect credentials or turn
    # server verification off. Exact first-path AWS commercial endpoint only.
    if (client.meta.endpoint_url != f"https://{service}.{region}.amazonaws.com"
            or client._endpoint.http_session._verify is not True):
        raise ValueError("verified fixed AWS endpoint required")


def _exact(items, field, value):
    if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
        raise ValueError("invalid destination listing")
    matches = [item for item in items if item.get(field) == value]
    if len(matches) != 1:
        raise ValueError("registered destination missing or ambiguous")
    return matches[0]


def destination_preflight(binding, *, logs, kms, clock=lambda: datetime.now(UTC),
                          monotonic=time.monotonic) -> dict:
    """Two bounded readbacks; not an authenticated registration or delivery proof.

    Account/region comes from exact resource ARNs. Credential subject must be
    authenticated by the host separately. SDK read permissions are separate from
    writer_policy; no credentials are rendered into the collector config.
    """
    if not isinstance(binding, CloudWatchAuditRegistration):
        raise ValueError("trusted CloudWatch registration required")
    _checked(logs, "logs", binding.region)
    _checked(kms, "kms", binding.region)

    started = monotonic()

    def bounded(call, **kwargs):
        if monotonic() - started >= 60:
            raise TimeoutError("destination readback deadline")
        result = call(**kwargs)
        if monotonic() - started >= 60:
            raise TimeoutError("destination readback deadline")
        return result

    def read():
        group_response = bounded(logs.describe_log_groups, logGroupNamePrefix=binding.group, limit=50)
        stream_response = bounded(logs.describe_log_streams,
            logGroupName=binding.group, logStreamNamePrefix=binding.stream, limit=50)
        key_response = bounded(kms.describe_key, KeyId=binding.kms_key_arn)
        for result in (group_response, stream_response, key_response):
            if (not isinstance(result, dict)
                    or result.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
                    or result.get("nextToken")):
                raise ValueError("destination readback incomplete")
        group = _exact(group_response.get("logGroups"), "logGroupName", binding.group)
        stream = _exact(stream_response.get("logStreams"), "logStreamName", binding.stream)
        key = key_response.get("KeyMetadata", {})
        if (group.get("arn") != binding.log_group_arn + ":*"
                or group.get("kmsKeyId") != binding.kms_key_arn
                or group.get("retentionInDays") != binding.retention_days
                or group.get("logGroupClass", "STANDARD") != "STANDARD"
                or stream.get("arn") != binding.log_group_arn + ":log-stream:" + binding.stream
                or not isinstance(key, dict)
                or key.get("Arn") != binding.kms_key_arn
                or key.get("KeyState") != "Enabled" or key.get("Enabled") is not True
                or key.get("KeyUsage") != "ENCRYPT_DECRYPT"
                or key.get("KeySpec") != "SYMMETRIC_DEFAULT"):
            raise ValueError("destination protection or ownership mismatch")
        dates = (group.get("creationTime"), stream.get("creationTime"))
        if any(type(x) is not int or x <= 0 for x in dates):
            raise ValueError("destination creation identity missing")
        return dates

    try:
        first, second = read(), read()
    except Exception:
        # Do not propagate provider diagnostics (resource identifiers/credentials).
        raise ValueError("CloudWatch audit destination not qualified") from None
    if first != second:
        raise ValueError("CloudWatch audit destination changed during preflight")
    collected = clock()
    if collected.tzinfo is None or collected.utcoffset().total_seconds() != 0:
        raise ValueError("UTC qualification time required")
    return {"registration_digest": binding.registration_digest,
            "checked_at": collected.isoformat(), "group_created_ms": first[0],
            "stream_created_ms": first[1], "configuration_ready": True,
            "delivery_verified": False, "gate_eligible": False}
