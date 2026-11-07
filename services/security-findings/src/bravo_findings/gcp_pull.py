"""Explicit Security Command Center pull entrypoint. Workload identity federation only: no service
account keys, and no SCC activation, sources or findings created."""

import argparse
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

import requests
from google.auth import identity_pool
from google.auth.transport.requests import AuthorizedSession, Request

from bravo_findings.collectors import run_once
from bravo_findings.gcp_scc import PARENT_RE, SccSource
from bravo_findings.ingest import Ingestor, collection_error_code
from bravo_findings.normalize import Normalizer
from bravo_findings.rules import Rules
from bravo_findings.store import Store
from bravo_findings.view import View

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"
AUDIENCE_RE = re.compile(
    r"//iam\.googleapis\.com/projects/[0-9]{6,20}/locations/global/"
    r"workloadIdentityPools/[a-z0-9-]{4,32}/providers/[a-z0-9-]{4,32}"
)
SERVICE_ACCOUNT_RE = re.compile(
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com"
)
IMPERSONATION_RE = re.compile(
    r"https://iamcredentials\.googleapis\.com/v1/projects/-/serviceAccounts/"
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com"
    r":generateAccessToken"
)


def federated_credentials(info):
    """A workload identity pool fed by a projected token file (the platform's OIDC issuer). Service
    account keys, user credentials, client secrets and command or URL token sources are refused."""
    if not isinstance(info, dict) or not isinstance(info.get("credential_source"), dict):
        raise PermissionError("federated_credentials_required")
    source, impersonation = info["credential_source"], info.get("service_account_impersonation_url")
    if (
        info.get("type") != "external_account"
        or info.get("token_url") != STS_TOKEN_URL
        or info.get("subject_token_type") != "urn:ietf:params:oauth:token-type:jwt"
        or not isinstance(info.get("audience"), str)
        or not AUDIENCE_RE.fullmatch(info["audience"])
        or info.get("universe_domain", "googleapis.com") != "googleapis.com"
        or {"client_id", "client_secret", "workforce_pool_user_project"} & set(info)
        or set(source) - {"file", "format"}
        or not isinstance(source.get("file"), str)
        or (impersonation is not None and not IMPERSONATION_RE.fullmatch(str(impersonation)))
    ):
        raise PermissionError("federated_credentials_required")
    return identity_pool.Credentials.from_info(info, scopes=SCOPES)


def expected_scope(scopes, environment_id):
    """The deploy-time scope of one environment: the SCC parent, the workload identity provider and the
    reader service account. The operator picks only the environment, so a parent or federation the
    reader can also see is never recorded as this environment (#915 review). Numbers stay out of Git:
    the file is mounted from a Secret or ConfigMap at run time."""
    entry = scopes.get(environment_id) if isinstance(scopes, dict) else None
    if (
        not isinstance(entry, dict)
        or set(entry) != {"parent", "audience", "service_account"}
        or not isinstance(entry["parent"], str)
        or not PARENT_RE.fullmatch(entry["parent"])
        or not isinstance(entry["audience"], str)
        or not AUDIENCE_RE.fullmatch(entry["audience"])
        or not isinstance(entry["service_account"], str)
        or not SERVICE_ACCOUNT_RE.fullmatch(entry["service_account"])
    ):
        raise PermissionError("scc_scope_not_registered")
    return entry


def bound_credentials(info, scope):
    """Federated credentials whose provider and impersonated reader are exactly the registered ones."""
    credentials = federated_credentials(info)
    impersonation = (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{scope['service_account']}:generateAccessToken"
    )
    if info["audience"] != scope["audience"] or info.get("service_account_impersonation_url") != impersonation:
        raise PermissionError("scc_federation_mismatch")
    return credentials


def authorized_session(credentials):
    # Like the AWS client config: no proxy or netrc from the environment, for API and token calls.
    token_session = requests.Session()
    token_session.trust_env = False
    session = AuthorizedSession(credentials, auth_request=Request(session=token_session))
    session.trust_env = False
    return session


def collect_scc(store, credentials_info, *, scopes, environment_id, clock, session_factory=None):
    source = "gcp_scc"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", environment_id):
        raise ValueError("invalid_environment_id")
    if store.source(source, environment_id) is None:
        store.register_source(source, environment_id)
    if not store.source(source, environment_id).enabled:
        return Counter(disabled=1)
    try:
        scope = expected_scope(scopes, environment_id)
        credentials = bound_credentials(credentials_info, scope)
        session = (session_factory or authorized_session)(credentials)
        return run_once(Ingestor(store, Normalizer()), SccSource(session, scope["parent"]), environment_id, clock)
    except Exception as error:  # noqa: BLE001 — never echo credentials or raw provider errors
        store.record_failure(source, environment_id, clock(), collection_error_code(error))
        return Counter(failed=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument(
        "--scope-file",
        default=os.environ.get("BRAVO_SCC_SCOPE_FILE"),
        help="deploy-time JSON: {environment_id: {parent, audience, service_account}}",
    )
    parser.add_argument(
        "--credentials-file",
        default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        help="workload identity federation credential configuration (not a key)",
    )
    args = parser.parse_args()
    store = None
    try:
        clock = lambda: time.time_ns() // 1_000_000  # noqa: E731
        store = Store(args.database)
        info = json.loads(Path(args.credentials_file).read_text()) if args.credentials_file else None
        scopes = json.loads(Path(args.scope_file).read_text()) if args.scope_file else None
        counts = collect_scc(
            store, info, scopes=scopes, environment_id=args.environment_id, clock=clock
        )
        print(
            json.dumps(
                {
                    "counts": dict(counts),
                    "statuses": View(store, Rules()).statuses(
                        clock(), allowed={args.environment_id}
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 1 if counts["failed"] or counts["disabled"] else 0
    except Exception:  # noqa: BLE001 — startup errors can contain paths and SDK diagnostics
        print(json.dumps({"error": "COLLECTOR_UNAVAILABLE"}))
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
