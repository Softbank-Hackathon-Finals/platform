"""Explicit GuardDuty pull entrypoint. No IAM, detectors or sample findings created."""

import argparse
import json
import re
import time
from collections import Counter

import boto3
from botocore.config import Config

from bravo_findings.collectors import run_once
from bravo_findings.guardduty import GuardDutySource
from bravo_findings.ingest import Ingestor, collection_error_code
from bravo_findings.normalize import Normalizer
from bravo_findings.rules import Rules
from bravo_findings.store import Store
from bravo_findings.view import View


def collect_guardduty(
    store, session, *, account_id, region, environment_id, clock, detector_id=None
):
    """One explicitly selected source per environment; existing Hub data needs handover."""
    source = "aws_guardduty"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", environment_id):
        raise ValueError("invalid_environment_id")
    if store.source(source, environment_id) is None:
        # Refuse before registering: a mistaken call must not block the selected source.
        if store.source("aws_security_hub", environment_id) is not None:
            return Counter(failed=1)
        store.register_source(source, environment_id)
    if not store.source(source, environment_id).enabled:
        return Counter(disabled=1)
    try:
        if store.source("aws_security_hub", environment_id) is not None:
            raise ValueError("source_handover_required")
        if not re.fullmatch(r"[0-9]{12}", account_id) or not re.fullmatch(
            r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region
        ):
            raise ValueError("invalid_aws_scope")
        config = Config(
            ignore_configured_endpoint_urls=True,
            proxies={},
            connect_timeout=5,
            read_timeout=10,
            retries={"mode": "standard", "total_max_attempts": 3},
        )
        identity = session.client("sts", region_name=region, config=config).get_caller_identity()
        if identity.get("Account") != account_id:
            raise PermissionError("account_mismatch")
        client = session.client("guardduty", region_name=region, config=config)
        pull = GuardDutySource(
            client, account_id=account_id, region=region, detector_id=detector_id
        )
        return run_once(Ingestor(store, Normalizer()), pull, environment_id, clock)
    except Exception as error:  # noqa: BLE001 — never echo SDK credentials or raw errors
        store.record_failure(source, environment_id, clock(), collection_error_code(error))
        return Counter(failed=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--detector-id")
    args = parser.parse_args()
    store = None
    try:
        clock = lambda: time.time_ns() // 1_000_000  # noqa: E731
        store = Store(args.database)
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        counts = collect_guardduty(
            store,
            session,
            account_id=args.account_id,
            region=args.region,
            environment_id=args.environment_id,
            detector_id=args.detector_id,
            clock=clock,
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
