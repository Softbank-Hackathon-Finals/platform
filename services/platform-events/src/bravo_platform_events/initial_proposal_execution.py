"""One initial-purpose Actions request, followed by cleanup and a sealed receipt."""

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from bravo_agent.product_execution_contracts import REPOSITORY
from bravo_agent.subscription_product import stopped
from bravo_agent.subscription_product_caller import ActionsProductToken, _window
from bravo_agent.subscription_product_seal import seal_bound_receipt, validate_recipient
from bravo_agent.subscription_product_tls import client_context

from .initial_proposal import InitialIntent, InitialResult
from .initial_proposal_runtime import InitialBrokerRequest
from .initial_proposal_transport import (
    INITIAL_WORKFLOW,
    initial_receipt_binding,
    validate_initial_receipt,
)


def workflow_boundary(intent, env):
    expected = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REPOSITORY_ID": "1443045223", "GITHUB_REPOSITORY_OWNER_ID": "350259663",
        "GITHUB_REF": "refs/heads/develop", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/{INITIAL_WORKFLOW}@refs/heads/develop",
        "GITHUB_SHA": intent.runtime_source_sha, "PRODUCT_WORKFLOW_SHA": intent.runtime_source_sha,
        "GITHUB_RUN_ATTEMPT": "1"}
    if (any(env.get(key) != value for key, value in expected.items())
            or re.fullmatch(r"[1-9][0-9]{0,19}", env.get("GITHUB_RUN_ID", "")) is None):
        raise ValueError("initial_workflow_identity_mismatch")


@dataclass(frozen=True)
class InitialTunnelCall:
    ca_pem: str = field(repr=False)
    transport: object = field(default=None, repr=False)

    def __post_init__(self):
        client_context(self.ca_pem)

    def __call__(self, body):
        try:
            request = InitialBrokerRequest.model_validate(body)
            with httpx.Client(verify=client_context(self.ca_pem), transport=self.transport,
                              trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(260, connect=5)) as client:
                with client.stream("POST", "https://127.0.0.1:18762/run", json=request.model_dump()) as response:
                    if response.status_code != 200:
                        return response.status_code, stopped("INITIAL_CALL_UNCONFIRMED")
                    raw = bytearray()
                    for part in response.iter_bytes(chunk_size=65536):
                        raw.extend(part)
                        if len(raw) > 1024 * 1024:
                            raise ValueError()
            result = InitialResult.model_validate_json(bytes(raw))
            return 200, result.model_dump(mode="json")
        except Exception:
            return 502, stopped("INITIAL_CALL_UNCONFIRMED")


@contextmanager
def initial_window(instance_id, *, ca_pem, ssm=None):
    with _window(instance_id, InitialTunnelCall(ca_pem), ssm=ssm) as call:
        yield call


def execute(intent, env, *, token_supplier=None, window=None):
    workflow_boundary(intent, env)
    validate_recipient(intent.recipient_public_key)
    receipt = initial_receipt_binding(intent, env["GITHUB_RUN_ID"]) | {
        "status": 502, "cleanup_confirmed": False, "result": stopped("INITIAL_EXECUTION_UNCONFIRMED")}
    try:
        ca = env.get("PRODUCT_BROKER_CA_PEM", "")
        client_context(ca)
        token = (token_supplier or ActionsProductToken(env))()
        body = InitialBrokerRequest(intent=intent, github_run_id=env["GITHUB_RUN_ID"], web_identity_token=token)
        with (window or initial_window)(env["BRAVO_INSTANCE_ID"], ca_pem=ca) as call:
            status, result = call(body.model_dump())
        receipt["cleanup_confirmed"] = True
        candidate = receipt | {"status": status, "result": result}
        validate_initial_receipt(intent, env["GITHUB_RUN_ID"], candidate)
        return candidate
    except Exception:
        return receipt


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args(argv)
    try:
        intent = InitialIntent.model_validate_json(os.environ["PRODUCT_INTENT"])
        workflow_boundary(intent, os.environ)
        validate_recipient(intent.recipient_public_key)
        client_context(os.environ.get("PRODUCT_BROKER_CA_PEM", ""))
        if args.preflight:
            return 0
        receipt = execute(intent, os.environ)
        sealed = seal_bound_receipt(intent.recipient_public_key,
                                   initial_receipt_binding(intent, os.environ["GITHUB_RUN_ID"]), receipt)
        with Path("product-result.json").open("x") as stream:
            json.dump(sealed, stream, allow_nan=False)
        Path("product-result.json").chmod(0o600)
        validate_initial_receipt(intent, os.environ["GITHUB_RUN_ID"], receipt)
        return 0
    except Exception:
        print("Initial execution unconfirmed; read this same execution only.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
