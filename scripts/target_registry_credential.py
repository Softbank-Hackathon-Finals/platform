"""Issue a temporary exact-repository pull credential in operator memory.

This module has no credential-printing CLI. A trusted operator supplies its
protected transport and passes only the returned registry authorization to it.
"""

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

REGION = "ap-northeast-2"
PULL = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
AUTH = "ecr:GetAuthorizationToken"


class Failure(RuntimeError):
    """Fixed public category; never include provider text."""


@dataclass(repr=False)
class Material:
    authorization: str = field(repr=False)
    registry: str
    session_expires_at: str
    registry_expires_at: str
    refresh_before: str
    policy: dict


def aws_arn(service, account, resource, region=""):
    parts = ("arn", "aws", service, region, account, resource)
    return ":".join(parts)


def repository_boundary(contract):
    account = contract.get("account_id")
    if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
        raise Failure("account_invalid")
    registry = f"{account}.dkr.ecr.{REGION}.amazonaws.com"
    arn = aws_arn("ecr", account, "repository/bravo/demo-chat", REGION)
    if (contract.get("region") != REGION or contract.get("repository_arn") != arn
            or contract.get("repository_url") != registry + "/bravo/demo-chat"):
        raise Failure("repository_boundary_invalid")
    return account, registry, arn


def session_policy(repository_arn):
    # Explicit denials also constrain grants from resource-based policies.
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": AUTH, "Resource": "*"},
        {"Effect": "Allow", "Action": PULL, "Resource": repository_arn},
        {"Effect": "Deny", "NotAction": [AUTH, *PULL], "Resource": "*"},
        {"Effect": "Deny", "Action": PULL, "NotResource": repository_arn},
    ]}


def aws(service, operation, parameters, credentials=None):
    if service not in ("sts", "ecr"):
        raise Failure("service_not_allowed")
    env = os.environ.copy()
    if credentials is not None:
        # Do not let an inherited profile, web identity or alternate credential
        # source replace the narrowed session used for registry authorization.
        env = {k: v for k, v in env.items() if not k.startswith("AWS_")}
        env.update(AWS_ACCESS_KEY_ID=credentials["AccessKeyId"],
                   AWS_SECRET_ACCESS_KEY=credentials["SecretAccessKey"],
                   AWS_SESSION_TOKEN=credentials["SessionToken"])
    env.update(AWS_MAX_ATTEMPTS="1", AWS_EC2_METADATA_DISABLED="true")
    command = ["aws", service, operation, "--region", REGION,
               "--endpoint-url", f"https://{service}.{REGION}.amazonaws.com",
               "--output", "json", "--no-cli-pager", "--cli-connect-timeout", "10",
               "--cli-read-timeout", "20", "--cli-input-json", json.dumps(parameters)]
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True,
                                timeout=40, check=False)
        if result.returncode:
            raise Failure("provider_request_failed")
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise TypeError
        return data
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        raise Failure("provider_response_unknown") from None


def expiry(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, TypeError):
        raise Failure("expiry_invalid") from None


def issue(contract, request=aws, now=None):
    account, registry, arn = repository_boundary(contract)
    started = now or datetime.now(UTC)
    identity = request("sts", "get-caller-identity", {})
    if identity.get("Account") != account or not str(identity.get("Arn", "")).startswith(aws_arn("iam", account, "user/")):
        raise Failure("operator_identity_invalid")
    policy = session_policy(arn)
    name = "t7-target-" + uuid.uuid4().hex[:16]
    response = request("sts", "get-federation-token", {
        "Name": name, "DurationSeconds": 3600, "Policy": json.dumps(policy, separators=(",", ":")),
    })
    credentials = response.get("Credentials", {})
    if not all(isinstance(credentials.get(k), str) and credentials[k] for k in
               ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")):
        raise Failure("session_response_invalid")
    if response.get("FederatedUser", {}).get("Arn") != aws_arn("sts", account, "federated-user/" + name):
        raise Failure("session_identity_invalid")
    session_end = expiry(credentials["Expiration"])
    if not 300 < (session_end - started).total_seconds() <= 3660:
        raise Failure("session_expiry_invalid")
    narrowed = request("sts", "get-caller-identity", {}, credentials)
    if narrowed.get("Account") != account or narrowed.get("Arn") != response["FederatedUser"]["Arn"]:
        raise Failure("narrowed_identity_invalid")
    result = request("ecr", "get-authorization-token", {}, credentials).get("authorizationData")
    if not isinstance(result, list) or len(result) != 1:
        raise Failure("registry_response_invalid")
    auth = result[0]
    if auth.get("proxyEndpoint") != "https://" + registry:
        raise Failure("registry_endpoint_invalid")
    registry_end = expiry(auth.get("expiresAt"))
    if not 300 < (registry_end - started).total_seconds() <= 43260:
        raise Failure("registry_expiry_invalid")
    authorization = auth.get("authorizationToken")
    if not isinstance(authorization, str) or not authorization:
        raise Failure("registry_authorization_invalid")
    return Material(authorization, registry, session_end.isoformat(), registry_end.isoformat(),
                    min(session_end, registry_end).isoformat(), policy)
