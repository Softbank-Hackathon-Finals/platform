#!/usr/bin/env python3
"""Standard Kubernetes exec credentials; no private key, token file or refresh daemon.

Installation binds a renewable workload identity to one existing ServiceAccount.
An auth proxy mints short-lived, Secret-bound tokens; the noauth proxy preserves
the resulting bearer identity and later impersonation checks.
"""

import argparse
import hashlib
import http.client
import json
import re
import socket
import ssl
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

SSR = "/apis/authentication.k8s.io/v1/selfsubjectreviews"
TTL = 600


@dataclass(frozen=True)
class Endpoint:
    server: str
    tls_server_name: str

    def __post_init__(self):
        parsed = urlsplit(self.server)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment
                or parsed.port not in (None, 443)
                or re.fullmatch(r"[a-z0-9.-]+", parsed.hostname) is None
                or re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net", self.tls_server_name) is None):
            raise ValueError("verified HTTPS endpoint and MagicDNS TLS name required")


@dataclass(frozen=True)
class Registration:
    """One operator registration, never fields supplied by an AI proposal."""

    cluster_uid: str
    namespace: str
    service_account: str
    service_account_uid: str
    issuer_group: str
    audience: str
    issuer: Endpoint
    target: Endpoint

    def __post_init__(self):
        for value in (self.cluster_uid, self.service_account_uid):
            if not isinstance(value, str) or str(UUID(value)) != value:
                raise ValueError("observed Kubernetes UID required")
        for value, maximum in ((self.namespace, 63), (self.service_account, 253)):
            if (not isinstance(value, str) or not 0 < len(value) <= maximum
                    or any(not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", label)
                           for label in value.split("."))
                    or (maximum == 63 and "." in value)):
                raise ValueError("valid namespace and ServiceAccount names required")
        if not re.fullmatch(r"tag:[a-z0-9-]+", self.issuer_group):
            raise ValueError("dedicated workload tag required")
        if not isinstance(self.audience, str) or not self.audience or len(self.audience) > 256:
            raise ValueError("registered API audience required")
        if not isinstance(self.issuer, Endpoint) or not isinstance(self.target, Endpoint):
            raise TypeError("validated endpoints required")
        if self.issuer.tls_server_name == self.target.tls_server_name:
            raise ValueError("auth issuer and noauth target must be distinct")

    @classmethod
    def parse(cls, value):
        # The constructor is the single input definition. Unknown/missing fields fail.
        return cls(**(value | {key: Endpoint(**value[key]) for key in ("issuer", "target")}))

    @property
    def name(self):
        identity = ["target-credential-v1", self.cluster_uid, self.namespace, self.service_account]
        return "credential-" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]

    @property
    def subject(self):
        return f"system:serviceaccount:{self.namespace}:{self.service_account}"


class APIError(RuntimeError):
    """Never include provider bodies, headers or token values in exceptions."""


def request(endpoint, method, path, body=None, *, token=None):
    parsed = urlsplit(endpoint.server)
    connection = http.client.HTTPSConnection(parsed.hostname, 443, timeout=20)
    context = ssl.create_default_context()
    try:
        raw = socket.create_connection((parsed.hostname, 443), timeout=20)
        try:
            connection.sock = context.wrap_socket(raw, server_hostname=endpoint.tls_server_name)
        except Exception:
            raw.close()
            raise
        headers = {"Content-Type": "application/json", "Host": endpoint.tls_server_name}
        if token:
            headers["Authorization"] = "Bearer " + token
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        if response.status not in (200, 201):
            raise APIError(f"credential API returned HTTP {response.status}")
        data = response.read(65537)
        if len(data) > 65536:
            raise APIError("credential API response too large")
        return json.loads(data)
    except APIError:
        raise
    except ssl.SSLCertVerificationError:
        raise APIError("credential API TLS validation failed") from None
    except TimeoutError:
        raise APIError("credential API connection timed out") from None
    except socket.gaierror:
        raise APIError("credential API name resolution failed") from None
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise APIError("credential API transport or response failed (" + type(error).__name__ + ")") from None
    finally:
        connection.close()


def acquire(registration, anchor_uid, *, call=request, now=None):
    """Mint on demand, then verify the actual bearer identity at the target API."""
    r = registration
    if not isinstance(anchor_uid, str) or str(UUID(anchor_uid)) != anchor_uid:
        raise ValueError("observed revocation anchor UID required")
    info = {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"}
    identity = call(r.issuer, "POST", SSR, info)["status"]["userInfo"]
    if r.issuer_group not in identity.get("groups", []):
        raise APIError("unexpected credential issuer identity")
    cluster = call(r.issuer, "GET", "/api/v1/namespaces/kube-system")
    if cluster["metadata"]["uid"] != r.cluster_uid:
        raise APIError("target cluster changed")
    base = f"/api/v1/namespaces/{r.namespace}"
    account = call(r.issuer, "GET", base + "/serviceaccounts/" + r.service_account)
    anchor = call(r.issuer, "GET", base + "/secrets/" + r.name)
    if (account["metadata"]["uid"] != r.service_account_uid
            or anchor["metadata"]["uid"] != anchor_uid
            or "deletionTimestamp" in account["metadata"] or "deletionTimestamp" in anchor["metadata"]):
        raise APIError("credential enrollment revoked or replaced")
    body = {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest", "spec": {
        "audiences": [r.audience], "expirationSeconds": TTL,
        "boundObjectRef": {"apiVersion": "v1", "kind": "Secret", "name": r.name, "uid": anchor_uid}}}
    status = call(r.issuer, "POST", base + "/serviceaccounts/" + r.service_account + "/token", body)["status"]
    now = now or datetime.now(UTC)
    expiry = datetime.fromisoformat(status["expirationTimestamp"])
    if expiry.tzinfo is None or not timedelta(seconds=90) < expiry - now <= timedelta(seconds=TTL + 30):
        raise APIError("unexpected token lifetime")
    token = status["token"]
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise APIError("invalid token response")
    bearer = call(r.target, "POST", SSR, info, token=token)["status"]["userInfo"]
    if bearer.get("username") != r.subject or bearer.get("uid") != r.service_account_uid:
        raise APIError("target proxy did not preserve ServiceAccount identity")
    return {"apiVersion": "client.authentication.k8s.io/v1", "kind": "ExecCredential",
            "status": {"token": token, "expirationTimestamp": (expiry - timedelta(seconds=60)).isoformat()}}


def kubeconfig(registration, configuration_file, executable):
    for value in (configuration_file, executable):
        if not Path(value).is_absolute():
            raise ValueError("absolute installation paths required")
    r = registration
    return {"apiVersion": "v1", "kind": "Config", "current-context": r.name,
            "clusters": [{"name": r.name, "cluster": {
                "server": r.target.server, "tls-server-name": r.target.tls_server_name}}],
            "contexts": [{"name": r.name, "context": {"cluster": r.name, "user": r.name}}],
            "users": [{"name": r.name, "user": {"exec": {
                "apiVersion": "client.authentication.k8s.io/v1", "command": executable,
                "args": ["exec", configuration_file], "interactiveMode": "Never"}}}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("exec", "render", "kubeconfig"))
    parser.add_argument("configuration")
    args = parser.parse_args()
    try:
        configuration = json.loads(Path(args.configuration).read_text())
        registration = Registration.parse(configuration["registration"])
        if args.operation == "exec":
            output = acquire(registration, configuration["anchor_uid"])
        elif args.operation == "kubeconfig":
            output = kubeconfig(registration, str(Path(args.configuration).resolve()), str(Path(__file__).resolve()))
        else:
            from credential_install import render
            output = render(registration)
        print(json.dumps(output))
    except (APIError, OSError, ValueError, TypeError, KeyError):
        # stdout is reserved for client-go's credential protocol, never diagnostics.
        print("credential acquisition or configuration failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
