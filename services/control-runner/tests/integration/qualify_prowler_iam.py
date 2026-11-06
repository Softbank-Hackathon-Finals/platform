"""Read-only live qualification; requires a separately installed Prowler 5.47.0.

Run in a private working directory with the trusted registration described in
docs/prowler-iam.md. This is not a public API or a complete IAM-01.2 collector.
The parent enforces a 180-second deadline; no fixer/cloud upload is invoked.
"""

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import logging
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from bravo_controls.aws_identity import RegisteredRole
from bravo_controls.prowler_iam import (
    CHECKS,
    VERSION,
    assess_iam_collection,
    read_role_inventory,
)
from bravo_controls.tool_reports import CheckMapping, ToolRun


def worker(profile, registration_path, output):
    import boto3
    from botocore.config import Config
    from prowler.lib.outputs.finding import Finding
    from prowler.lib.outputs.ocsf.ocsf import OCSF
    from prowler.providers.aws.aws_provider import AwsProvider
    from prowler.providers.common.models import Audit_Metadata

    if importlib.metadata.version("prowler") != VERSION:
        raise ValueError("pinned_scanner_required")
    raw_registration = registration_path.read_bytes()
    registration = json.loads(raw_registration)
    role = RegisteredRole(registration["role_arn"], registration["role_id"])
    scope = "sha256:" + hashlib.sha256(raw_registration).hexdigest()
    # A private operator-created registration, not a report-supplied policy.
    baseline = registration.get("approved_inventory_sha256", "")
    session = boto3.Session(profile_name=profile, region_name="ap-northeast-2")
    client = session.client(
        "iam",
        verify=True,
        config=Config(
            connect_timeout=5,
            read_timeout=10,
            retries={"total_max_attempts": 1},
            proxies={},
            ignore_configured_endpoint_urls=True,
        ),
    )
    started_at = datetime.now(UTC).isoformat()
    before = read_role_inventory(client, role)
    diagnostics = output / "diagnostics.private.log"
    with diagnostics.open("w") as log:
        # The SDK catches some analysis errors and logs them while returning
        # PASS. Capture warnings/errors as part of completeness, including
        # stdout/stderr. Do not suppress or pattern-filter these diagnostics.
        from contextlib import redirect_stderr, redirect_stdout

        handler = logging.StreamHandler(log)
        handler.setLevel(logging.WARNING)
        logger = logging.getLogger()
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            with redirect_stdout(log), redirect_stderr(log):
                provider = AwsProvider(
                    profile=profile,
                    regions={"ap-northeast-2"},
                    resource_arn=[role.arn],
                    retries_max_attempts=0,
                    connect_timeout=5,
                    read_timeout=10,
                )
                provider.audit_metadata = Audit_Metadata(
                    services_scanned=0,
                    expected_checks=list(CHECKS),
                    completed_checks=0,
                    audit_progress=0,
                )
                findings = []
                for check_id in CHECKS:
                    module = importlib.import_module(
                        f"prowler.providers.aws.services.iam.{check_id}.{check_id}"
                    )
                    findings.extend(
                        Finding.generate_output(provider, row, SimpleNamespace())
                        for row in getattr(module, check_id)().execute()
                    )
                output_rows = OCSF(findings).data
                raw = json.dumps(
                    [r.model_dump(mode="json") for r in output_rows], sort_keys=True
                ).encode()
        finally:
            logger.removeHandler(handler)
    after = read_role_inventory(client, role)
    client.close()
    (output / "report.private.json").write_bytes(raw)
    tool_run = ToolRun("prowler", VERSION, scope, role.arn, raw, 0, True)
    mapping = CheckMapping(
        "prowler", VERSION, scope, "candidate-aws-inline-v1", tuple((c, role.arn) for c in CHECKS)
    )
    result = assess_iam_collection(
        tool_run,
        mapping,
        before=before,
        after=after,
        baseline_digest=baseline,
        diagnostics=diagnostics.read_bytes(),
    )
    summary = {
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "tool": "prowler",
        "version": VERSION,
        "sdk_invocation": True,
        "same_inventory": before == after,
        "inventory_sha256": before.digest,
        "report_sha256": result.output_sha256,
        "rows": len(output_rows),
        "observed": result.observed,
        "unknown_reason": result.unknown_reason,
        "summary": result.summary,
        "full_iam_01_2_qualified": False,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--registration", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    if args.worker:
        try:
            worker(args.profile, args.registration, args.output)
            return 0
        except Exception as error:
            # The wrapper retains a fixed error class only. It never prints
            # provider exceptions, policy documents or credentials.
            print(json.dumps({"result": "UNKNOWN", "error_type": type(error).__name__}))
            return 1
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("AWS_ENDPOINT_URL")
        and k not in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
    }
    env.update(AWS_EC2_METADATA_DISABLED="true", AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true")
    try:
        with (args.output / "worker.private.log").open("wb") as log:
            process = subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(__file__).resolve()),
                    "--profile",
                    args.profile,
                    "--registration",
                    str(args.registration.resolve()),
                    "--output",
                    str(args.output.resolve()),
                    "--worker",
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
                check=False,
            )
        if process.returncode != 0:
            print("UNKNOWN: scanner worker failed; inspect private worker log")
            return 1
        print((args.output / "summary.json").read_text())
        return 0
    except subprocess.TimeoutExpired:
        print("UNKNOWN: scanner deadline exceeded; no complete collection")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
