"""Bounded VM broker. Only a GitHub OIDC token crosses the SSM TLS tunnel."""

import contextlib
import io
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import boto3
from botocore.config import Config

from .contracts import FailureBundle, SnapshotManifest, digest
from .subscription_admin import run_worker
from .subscription_product import (
    ProductAccess,
    ProductService,
    ProposalJournal,
    invoke_product_worker,
)
from .subscription_store import SubscriptionStore
from .verification import validate_result


def execute_fixture(settings, web_token, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"subscription-[a-f0-9]{32}", run_id):
        raise ValueError("invalid execution id")
    config = Config(retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=20)
    # Web identity is validated by STS against the exact repository/ref trust policy.
    credentials = boto3.client("sts", region_name=settings["region"], config=config).assume_role_with_web_identity(
        RoleArn=settings["broker_role_arn"], RoleSessionName="bravo-subscription-vm",
        WebIdentityToken=web_token, DurationSeconds=900)["Credentials"]
    root = Path(settings["worker"])
    runs = Path(settings["state_dir"]).parent / "runs"
    runs.mkdir(mode=0o700, exist_ok=True)
    execution = runs / run_id
    execution.mkdir(mode=0o700)  # Reuse must fail, even after a failed worker call.
    client = boto3.client("secretsmanager", region_name=settings["region"], config=config,
                         aws_access_key_id=credentials["AccessKeyId"],
                         aws_secret_access_key=credentials["SecretAccessKey"],
                         aws_session_token=credentials["SessionToken"])
    store = SubscriptionStore(settings["secret_arn"], settings["runtime_host_id"], client=client)
    before, old_version = store.read()
    bundle = FailureBundle.model_validate({**json.loads((root / "examples/failure.json").read_text()),
                                           "run_id": run_id})
    bundle_path = execution / "bundle.json"
    with bundle_path.open("x") as stream:
        stream.write(bundle.model_dump_json())
    bundle_path.chmod(0o400)
    output = execution / "worker-result.json"
    args = SimpleNamespace(model=settings["model"], bundle=bundle_path,
                           manifest=root / "examples/snapshot-manifest.json",
                           snapshot=root / "examples/source", output=output)
    with contextlib.redirect_stdout(io.StringIO()):
        code = run_worker(store, Path(settings["state_dir"]), args)
    result = json.loads(output.read_text()) if output.exists() else {}
    result["input_kind"] = "synthetic_db_host_fixture"
    if code == 0:
        received_bundle, _ = validate_result(result)
        manifest = SnapshotManifest.model_validate_json(args.manifest.read_text())
        if received_bundle != bundle or result["snapshot_digest"] != digest(manifest):
            raise ValueError("worker returned a different input")
        published = execution / "proposal.json"
        with published.open("x") as stream:
            json.dump(result, stream)
        published.chmod(0o400)
    after, new_version = store.read()
    return {"state": result.get("state", "NEEDS_HUMAN"), "exit_code": code, "result": result,
            "runtime_source_sha": settings["source_sha"],
            "credential_rotated": old_version != new_version,
            "access_rotated": before.get("access_token") != after.get("access_token"),
            "refresh_rotated": before.get("refresh_token") != after.get("refresh_token"),
            "registration_reused": before.get("client_id") == after.get("client_id"),
            "subject_reused": before.get("subject") == after.get("subject"),
            "active_after": after["state"] == "ACTIVE", "uid_nonroot": os.getuid() != 0,
            "expires_at_increased": after.get("expires_at", 0) > before.get("expires_at", 0)}


def authenticate_product(settings, token):
    """STS validates the existing broker trust before either product input is read."""
    config = Config(retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=20)
    identity = boto3.client("sts", region_name=settings["region"], config=config).assume_role_with_web_identity(
        RoleArn=settings["broker_role_arn"], RoleSessionName="bravo-subscription-vm",
        WebIdentityToken=token, DurationSeconds=900)
    subject = {key: identity[key] for key in ("Provider", "Audience", "SubjectFromWebIdentityToken")}
    if not all(isinstance(value, str) and value for value in subject.values()):
        raise ValueError("missing authenticated principal")
    return ProductAccess(digest(subject), identity["Credentials"])


def product_service(settings, inputs, journal_root, *, request_inputs=None):
    """Host-only opt-in. No CLI setting can substitute an unauthenticated reader."""
    return ProductService(ProposalJournal(journal_root), lambda token: authenticate_product(settings, token), inputs,
                          lambda *args: invoke_product_worker(settings, *args), settings["source_sha"],
                          request_inputs=request_inputs)


def serve(settings, *, product=None, fixture_enabled=True, tls_context=None):
    if product is not None and tls_context is None:
        raise ValueError("authenticated product TLS listener required")
    completed = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(240)

        def respond(self, status, response):
            data = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            nonlocal completed
            product_route = self.path in {"/run", "/input/probe"}
            if product_route and product is None:
                # A trusted host must explicitly install the authenticated reader.
                self.respond(503, {"state": "NEEDS_HUMAN", "reason": "PRODUCT_INPUT_UNAVAILABLE"})
                return
            if (self.path not in {"/run", "/input/probe", "/validation/fixture"}
                    or (self.path == "/validation/fixture" and not fixture_enabled)):
                self.respond(404, {"state": "NEEDS_HUMAN", "reason": "UNKNOWN_ROUTE"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32768:
                    raise ValueError("invalid length")
                body = json.loads(self.rfile.read(size))
                if product_route:
                    completed += 1
                    status, response = product.probe(body) if self.path == "/input/probe" else product.run(body)
                    self.respond(status, response)
                    return
                if (not isinstance(body, dict) or set(body) != {"web_identity_token", "run_id"}
                        or not isinstance(body["web_identity_token"], str)):
                    raise ValueError("invalid fields")
            except (ValueError, RecursionError):
                reason = "INVALID_PRODUCT_REQUEST" if product_route else "INVALID_FIXTURE_REQUEST"
                self.respond(400, {"state": "NEEDS_HUMAN", "reason": reason})
                return
            completed += 1
            try:
                response = execute_fixture(settings, body["web_identity_token"], body["run_id"])
            except Exception:
                # SDK responses may include credentials; return a fixed error only.
                response = {"state": "NEEDS_HUMAN", "reason": "CONTROLLED_RUNTIME_FAILURE"}
            self.respond(200, response)

    with HTTPServer(("127.0.0.1", 18761), Handler) as server:
        if tls_context is not None:
            accept = server.get_request

            def authenticated_request():
                connection, address = accept()
                connection.settimeout(5)
                try:
                    return tls_context.wrap_socket(connection, server_side=True), address
                except Exception:
                    connection.close()
                    raise

            server.get_request = authenticated_request
        server.timeout = 1
        deadline = time.monotonic() + 600
        while completed < 2 and time.monotonic() < deadline:
            server.handle_request()


if __name__ == "__main__":
    import sys
    serve(json.loads(Path(sys.argv[1]).read_text()))
