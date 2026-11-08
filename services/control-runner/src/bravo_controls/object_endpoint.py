"""IAM-01.1 on a registered S3-compatible endpoint (the target's SeaweedFS), the AWS method unchanged.

The AWS object collector (`aws_probes.S3AnonymousReadProbe`) only talks to AWS regional endpoints
and checks a pinned object version. The on-prem target serves the same bucket from SeaweedFS, which
has no AWS account, no versioning and its own endpoint. This collector asks the same question with
the same judgment (`aws_probes._ReadProbe`): the app identity's signed GET must succeed, an unsigned
GET of the same object must be refused, and the app GET must still succeed afterwards (a missing
object can also answer 403 to an anonymous caller). Object contents are never read or kept.

The server registers the endpoint, bucket and key from the target registration and builds both
clients with `endpoint_clients` (path-style addressing, bounded timeouts and retries, verified TLS).
The app access key is the T9 `client` identity, handed over in memory by the server; it never
reaches the evidence, which carries only a hash of the identity label.
"""

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from .aws_probes import _ID, _client, _failure, _http, _ReadProbe
from .runner import Environment

TOOL = "bravo-controls-s3-endpoint"
TOOL_VERSION = "1"
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_LOOPBACK = {"localhost"}
# An in-cluster Service: the collector runs as a Job beside the store (the target's SeaweedFS serves plain
# HTTP inside the cluster). SigV4 still signs each request; the answer is trusted as far as the cluster network.
_SERVICE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\.svc(?:\.cluster\.local)?\Z")


def _endpoint(value: str) -> str:
    """https://host[:port]; plain http only to a loopback host (local qualification) or an in-cluster Service."""
    try:
        url = urlsplit(value)
        host, port = url.hostname, url.port
    except (TypeError, ValueError):
        raise ValueError("registered S3 endpoint required") from None
    if (not isinstance(value, str) or url.scheme not in {"https", "http"} or not host or url.username or url.password
            or url.path not in {"", "/"} or url.query or url.fragment or (port is not None and not 0 < port < 65536)):
        raise ValueError("registered S3 endpoint required")
    if url.scheme == "http":
        try:
            loopback = host in _LOOPBACK or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback and not _SERVICE.fullmatch(host):
            raise ValueError("plain HTTP is accepted for a loopback endpoint or an in-cluster Service only")
    return value.rstrip("/")


@dataclass(frozen=True)
class EndpointObjectTarget:
    app: str
    environment: Environment
    endpoint: str
    bucket: str
    key: str
    region: str = "us-east-1"

    def __post_init__(self):
        if (not isinstance(self.app, str) or not _ID.fullmatch(self.app)
                or not isinstance(self.environment, Environment)
                or not isinstance(self.bucket, str) or not _BUCKET.fullmatch(self.bucket)
                or not isinstance(self.key, str) or not 1 <= len(self.key.encode()) <= 1024
                or any(ord(c) < 32 for c in self.key)
                or not isinstance(self.region, str) or not re.fullmatch(r"[a-z0-9-]{3,40}", self.region)):
            raise ValueError("invalid registered S3-compatible object target")
        object.__setattr__(self, "endpoint", _endpoint(self.endpoint))


def endpoint_clients(target: EndpointObjectTarget, *, access_key_id: str, secret_access_key: str,
                     ca_bundle: str | None = None):
    """(app client, anonymous client) bound to the registered endpoint, never to the environment's."""
    import botocore.session
    from botocore import UNSIGNED
    from botocore.config import Config

    def config(**extra):
        return Config(connect_timeout=3, read_timeout=5, retries={"mode": "standard", "total_max_attempts": 2},
                      s3={"addressing_style": "path"}, proxies={}, **extra)

    # Not the operator's AWS_PROFILE (a missing profile would fail the client) nor AWS_CA_BUNDLE.
    session = botocore.session.Session(session_vars={"profile": (None, None, None, None),
                                                     "ca_bundle": ("ca_bundle", None, None, None)})
    session.set_config_variable("credentials_file", "/dev/null")  # only the credentials given here
    session.set_config_variable("config_file", "/dev/null")
    verify = ca_bundle if ca_bundle is not None else target.endpoint.startswith("https://")
    app = session.create_client("s3", region_name=target.region, endpoint_url=target.endpoint, verify=verify,
                                aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,
                                config=config(signature_version="s3v4"))
    anonymous = session.create_client("s3", region_name=target.region, endpoint_url=target.endpoint, verify=verify,
                                      config=config(signature_version=UNSIGNED))
    return app, anonymous


class EndpointAnonymousReadProbe(_ReadProbe):
    """IAM-01.1 against a registered S3-compatible endpoint: app GET, anonymous GET, app GET again."""

    test_id = "IAM-01.1"
    action = "s3:GetObject"
    _recheck_denial = True
    tool, tool_version = TOOL, TOOL_VERSION

    def __init__(self, target: EndpointObjectTarget, *, app_client, anonymous_client, app_identity: str,
                 evidence_sink: Callable[[bytes], None], clock=lambda: datetime.now(UTC)):
        if not isinstance(target, EndpointObjectTarget):
            raise ValueError("registered S3-compatible object target required")
        if not isinstance(app_identity, str) or not _ID.fullmatch(app_identity):
            raise ValueError("registered app identity label required")
        self._setup(target, app_client, anonymous_client, f"s3-endpoint:{app_identity}", "anonymous",
                    evidence_sink, clock)
        self._validate_clients()

    def _validate_clients(self):
        _client(self.app_client, "s3", self.target.region)
        _client(self.negative_client, "s3", self.target.region, anonymous=True)
        for client in (self.app_client, self.negative_client):
            meta: Any = client.meta
            if (meta.endpoint_url.rstrip("/") != self.target.endpoint
                    or (meta.config.s3 or {}).get("addressing_style") != "path"):
                raise ValueError("client is not bound to the registered endpoint")

    def _resource_binding(self):
        return {"endpoint": self.target.endpoint, "bucket": self.target.bucket, "key": self.target.key}

    def _read(self, client):
        try:
            response = client.get_object(Bucket=self.target.bucket, Key=self.target.key)
        except Exception as error:
            return _failure(error, secret=False)
        status = _http(response)
        body = response.get("Body") if isinstance(response, dict) else None
        valid = status == 200 and callable(getattr(body, "close", None))
        # GET authorization is enough; never read, log, hash or persist object contents.
        if callable(getattr(body, "close", None)):
            try:
                body.close()
            except Exception:
                valid = False
        return {"outcome": "allow" if valid else None, "http_status": status,
                "reason": None if valid else "UNEXPECTED_RESPONSE"}
