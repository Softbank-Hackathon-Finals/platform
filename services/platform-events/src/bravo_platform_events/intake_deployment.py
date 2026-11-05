"""Offline, explicit configuration for the T15 -> T10 workload boundary.

This module neither fetches keys nor installs evidence/hold authorities. It does
not mint tokens, create Kubernetes resources, or print credential contents.
"""

import json
import re
from pathlib import Path

from jwt.algorithms import RSAAlgorithm
from jwt.exceptions import InvalidKeyError

from .workload_identity import WorkloadSender

AUDIENCE = "bravo-t10-rehearsal-intake"
TOKEN_PATH = "/var/run/secrets/t10-intake/token"
_DNS = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_FIELDS = {"version", "issuer", "audience", "namespace", "service_account", "tenant_id", "key_id"}


def sender_from_config(config: dict, public_jwks: dict) -> WorkloadSender:
    """Select a key using trusted deployment config, never a token's header/URL.

The caller must obtain the public JWKS through the cluster's authenticated TLS
channel and mount it read-only. Rotation requires replacing that reviewed config.
"""
    try:
        if (type(config) is not dict or set(config) != _FIELDS
                or type(config["version"]) is not int or config["version"] != 1
                or any(type(config[k]) is not str or not config[k] for k in _FIELDS - {"version"})
                or config["audience"] != AUDIENCE
                or not all(_DNS.fullmatch(config[k]) for k in ("namespace", "service_account"))):
            raise ValueError()
        if type(public_jwks) is not dict or set(public_jwks) != {"keys"} or type(public_jwks["keys"]) is not list:
            raise ValueError()
        keys = [key for key in public_jwks["keys"] if type(key) is dict and key.get("kid") == config["key_id"]]
        if len(keys) != 1:
            raise ValueError()
        key = keys[0]
        if (key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256"
                or key.get("use", "sig") != "sig" or key.get("key_ops", ["verify"]) != ["verify"]
                or not {"kty", "kid", "n", "e"} <= set(key)
                or set(key) - {"kty", "kid", "n", "e", "alg", "use", "key_ops"}):
            raise ValueError()
        return WorkloadSender(config["issuer"], AUDIENCE,
            f"system:serviceaccount:{config['namespace']}:{config['service_account']}",
            RSAAlgorithm.from_jwk(json.dumps(key)), config["tenant_id"])
    except (ValueError, TypeError, KeyError, InvalidKeyError):
        raise ValueError("invalid_intake_deployment") from None


def sender_pod_patch(config: dict, public_jwks: dict, *, deployment: str, container: str) -> dict:
    """Strategic merge patch for one dedicated trusted-host Deployment.

Only the named host container receives this volume. Image, entry point, target
API credentials, network policy and evidence ports belong to the host deployment.
"""
    sender_from_config(config, public_jwks)
    if not all(isinstance(value, str) and _DNS.fullmatch(value) for value in (deployment, container)):
        raise ValueError("invalid_intake_deployment")
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": deployment, "namespace": config["namespace"]},
        "spec": {"template": {"spec": {
            "serviceAccountName": config["service_account"],
            "automountServiceAccountToken": False,
            "containers": [{"name": container, "volumeMounts": [{
                "name": "t10-intake-token", "mountPath": str(Path(TOKEN_PATH).parent), "readOnly": True}]}],
            "volumes": [{"name": "t10-intake-token", "projected": {"sources": [{
                "serviceAccountToken": {"audience": AUDIENCE, "expirationSeconds": 600, "path": "token"}}]}}],
        }}},
    }


class ProjectedToken:
    """Reload kubelet's rotating token on every send; never retain it in config."""

    def __init__(self, path=TOKEN_PATH):
        self.path = Path(path)

    def __call__(self):
        try:
            with self.path.open("r", encoding="ascii") as stream:
                raw = stream.read(16385)
            value = raw.strip()
            if not value or len(raw) > 16384 or any(c.isspace() for c in value):
                raise ValueError()
            return value
        except (OSError, UnicodeError, ValueError):
            raise ValueError("projected_token_unavailable") from None
