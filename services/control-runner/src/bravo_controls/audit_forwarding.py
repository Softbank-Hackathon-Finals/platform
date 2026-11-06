"""Render an environment-local OTel audit pipeline from operator registration.

Pure configuration generation, not authentication, discovery, or deployment.
The caller verifies pod UID/node ownership and destination identity beforehand.
"""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_HASH = re.compile(r"sha256:[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class AuditFileRegistration:
    environment: str
    destination_environment: str
    registration_digest: str
    namespace: str
    pod: str
    pod_uid: str
    node_uid: str

    def __post_init__(self):
        for value in (self.environment, self.destination_environment, self.namespace, self.pod):
            if not isinstance(value, str) or not _NAME.fullmatch(value):
                raise ValueError("registered DNS labels required")
        if self.environment != self.destination_environment:
            raise ValueError("audit logs must remain in their environment")
        if not isinstance(self.registration_digest, str) or not _HASH.fullmatch(self.registration_digest):
            raise ValueError("registration digest required")
        for value in (self.pod_uid, self.node_uid):
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError("canonical registered UID required")


@dataclass(frozen=True)
class AuditForwardingRegistration(AuditFileRegistration):
    # A separately authenticated environment-local OTLP logs receiver.
    endpoint: str
    include_node_audit: bool = False

    def __post_init__(self):
        super().__post_init__()
        if type(self.include_node_audit) is not bool:
            raise ValueError("explicit node audit selection required")
        if not isinstance(self.endpoint, str):
            raise ValueError("registered HTTPS OTLP origin required")
        parsed = urlsplit(self.endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or any(ord(c) < 33 for c in self.endpoint)):
            raise ValueError("registered HTTPS OTLP origin required")
        # Never render metadata, loopback, unspecified or multicast endpoints.
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            if (parsed.hostname == "localhost" or parsed.hostname.endswith(".localhost")
                    or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname)):
                raise ValueError("registered receiver hostname required") from None
        else:
            if address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
                raise ValueError("receiver address is not an environment endpoint")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("invalid receiver port")


def _file_config(binding: AuditFileRegistration, *, include_node_audit: bool = False) -> dict:
    """No wildcard namespace/UID, app OTLP input, insecure TLS or cloud credentials."""
    if not isinstance(binding, AuditFileRegistration):
        raise ValueError("trusted registration required")
    common = {
        "start_at": "beginning", "storage": "file_storage/audit", "poll_interval": "1s",
        "max_log_size": "1MiB", "max_concurrent_files": 16,
        "include_file_name": False, "include_file_path": False,
        "retry_on_failure": {"enabled": True, "initial_interval": "1s",
                             "max_interval": "30s", "max_elapsed_time": "0s"},
    }
    receivers = {
        "filelog/cnpg": {
            **common,
            "include": [f"/var/log/pods/{binding.namespace}_{binding.pod}_{binding.pod_uid}/postgres/*.log*"],
            "exclude": ["/var/log/pods/**/*.gz"],
            "operators": [{"type": "container", "id": "cri", "add_metadata_from_filepath": False}],
        }
    }
    processors = {
        "memory_limiter": {"check_interval": "1s", "limit_mib": 96, "spike_limit_mib": 24},
        "transform/cnpg": {
            "error_mode": "propagate",
            "log_statements": [{"context": "log", "statements": [
                'set(cache, ParseJSON(body))',
                'keep_keys(cache, ["logger", "record"])',
                'delete_key(cache, "record") where not IsMap(cache["record"])',
                'keep_keys(cache["record"], ["user_name", "database_name", "application_name", "error_severity", "message", "audit"]) where IsMap(cache["record"])',
                'delete_key(cache["record"], "message") where IsMap(cache["record"]) and (cache["logger"] != "postgres" or not IsString(cache["record"]["message"]))',
                'delete_key(cache["record"], "message") where IsMap(cache["record"]) and IsString(cache["record"]["message"]) and not IsMatch(cache["record"]["message"], "^connection authorized: user=")',
                'delete_key(cache["record"], "audit") where IsMap(cache["record"]) and not IsMap(cache["record"]["audit"])',
                'keep_keys(cache["record"]["audit"], ["audit_type", "class", "object_type", "object_name", "command"]) where IsMap(cache["record"]["audit"])',
                'set(body, cache)',
                'keep_keys(attributes, [])',
            ]}],
        },
    }
    labels = {
        "bravo.environment": binding.environment,
        "bravo.audit.registration": binding.registration_digest,
        "k8s.node.uid": binding.node_uid,
    }
    pipelines = {}
    for kind in ("cnpg", "kubernetes") if include_node_audit else ("cnpg",):
        values = {**labels, "bravo.audit.source": kind}
        if kind == "cnpg":
            values.update({"k8s.namespace.name": binding.namespace, "k8s.pod.name": binding.pod,
                           "k8s.pod.uid": binding.pod_uid, "k8s.container.name": "postgres"})
        processors["transform/binding-" + kind] = {
            "error_mode": "propagate", "log_statements": [{"context": "log", "statements": [
                'keep_keys(resource.attributes, [])',
                *[f'set(resource.attributes["{key}"], "{value}")' for key, value in values.items()],
            ]}],
        }
        pipelines["logs/" + kind] = {
            "receivers": ["filelog/" + kind],
            "processors": ["memory_limiter", "transform/" + kind, "transform/binding-" + kind],
            "exporters": ["otlp_http/audit"],
        }
    if include_node_audit:
        receivers["filelog/kubernetes"] = {
            **common, "include": ["/var/log/bravo-audit/kubernetes/audit.log*"],
            "exclude": ["/var/log/bravo-audit/kubernetes/*.gz"],
        }
        processors["transform/kubernetes"] = {
            "error_mode": "propagate", "log_statements": [{"context": "log", "statements": [
                'set(cache, ParseJSON(body))',
                'keep_keys(cache, ["apiVersion", "kind", "auditID", "stage", "verb", "user", "impersonatedUser", "objectRef", "responseStatus", "requestReceivedTimestamp", "stageTimestamp"])',
                'delete_key(cache, "user") where not IsMap(cache["user"])',
                'delete_key(cache, "impersonatedUser") where not IsMap(cache["impersonatedUser"])',
                'delete_key(cache, "objectRef") where not IsMap(cache["objectRef"])',
                'delete_key(cache, "responseStatus") where not IsMap(cache["responseStatus"])',
                'keep_keys(cache["user"], ["username", "uid"]) where IsMap(cache["user"])',
                'keep_keys(cache["impersonatedUser"], ["username", "uid"]) where IsMap(cache["impersonatedUser"])',
                'keep_keys(cache["objectRef"], ["apiGroup", "resource", "subresource", "namespace", "name", "uid", "apiVersion", "resourceVersion"]) where IsMap(cache["objectRef"])',
                'keep_keys(cache["responseStatus"], ["code"]) where IsMap(cache["responseStatus"])',
                'set(body, cache)', 'keep_keys(attributes, [])',
            ]}],
        }
    return {
        "extensions": {"file_storage/audit": {
            "directory": "/var/lib/bravo-audit/checkpoints", "create_directory": False,
            "timeout": "1s", "fsync": True, "recreate": False, "max_size": 67108864,
        }},
        "receivers": receivers, "processors": processors,
        "exporters": {},
        "service": {
            "extensions": ["file_storage/audit"], "pipelines": pipelines,
            "telemetry": {"logs": {"level": "error"}, "metrics": {"readers": [{"pull": {
                "exporter": {"prometheus": {"host": "127.0.0.1", "port": 8888}}
            }}]}},
        },
    }


def collector_config(binding: AuditForwardingRegistration) -> dict:
    """Environment-local OTLP path; existing call contract preserved."""
    if not isinstance(binding, AuditForwardingRegistration):
        raise ValueError("trusted OTLP registration required")
    config = _file_config(binding, include_node_audit=binding.include_node_audit)
    config["exporters"] = {"otlp_http/audit": {
            "endpoint": binding.endpoint, "timeout": "10s",
            "tls": {"ca_file": "/var/run/bravo-audit/tls/ca.crt",
                    "cert_file": "/var/run/bravo-audit/tls/tls.crt",
                    "key_file": "/var/run/bravo-audit/tls/tls.key",
                    "min_version": "1.2", "insecure": False, "insecure_skip_verify": False},
            "retry_on_failure": {"enabled": True, "initial_interval": "1s", "max_interval": "30s", "max_elapsed_time": "0s"},
            "sending_queue": {"enabled": True, "storage": "file_storage/audit", "queue_size": 2048, "num_consumers": 2},
        }}
    return config
