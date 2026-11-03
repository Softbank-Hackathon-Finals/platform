#!/usr/bin/env python3
"""Verify an explicit operator SSO profile without emitting identity or token values."""
from __future__ import annotations

import argparse
import configparser
import json
import os
from pathlib import Path
import re
import subprocess


def inspect_profile(config: configparser.RawConfigParser, profile: str) -> str:
    section = "profile " + profile
    if not config.has_section(section):
        return "SSO_PROFILE_MISSING"
    session = config.get(section, "sso_session", fallback="")
    if not session or not config.has_section("sso-session " + session):
        return "SSO_SESSION_MISSING"
    sso = "sso-session " + session
    if not config.get(sso, "sso_start_url", fallback="").startswith("https://"):
        return "SSO_START_URL_MISSING"
    if not config.get(sso, "sso_region", fallback=""):
        return "SSO_REGION_MISSING"
    if any(config.has_option(section, key) for key in ("credential_process", "source_profile", "role_arn", "aws_access_key_id", "aws_secret_access_key")):
        return "MIXED_CREDENTIAL_SOURCES"
    return "CONFIGURED"


def check_identity(identity: dict, account: str, role_name: str) -> str:
    if identity.get("Account") != account:
        return "ACCOUNT_MISMATCH"
    role = re.escape(role_name)
    pattern = rf"arn:aws:sts::{account}:assumed-role/AWSReservedSSO_{role}_[0-9a-fA-F]{{16}}/[^/]+"
    return "SSO_SESSION_OBSERVED" if re.fullmatch(pattern, identity.get("Arn", "")) else "NOT_EXPECTED_SSO_ROLE"


def run(profile: str, account: str, config_path: Path) -> dict:
    config = configparser.RawConfigParser()
    config.read(config_path)
    status = inspect_profile(config, profile)
    if status != "CONFIGURED":
        return {"status": status, "sso_verified": False}
    section = "profile " + profile
    if config.get(section, "sso_account_id", fallback="") != account:
        return {"status": "PROFILE_ACCOUNT_MISMATCH", "sso_verified": False}
    role_name = config.get(section, "sso_role_name", fallback="")
    if not role_name:
        return {"status": "PERMISSION_SET_MISSING", "sso_verified": False}
    # The selected profile must resolve its own SSO cache; ambient access keys are excluded.
    env = {k: v for k, v in os.environ.items() if k not in {
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
        "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    }}
    env["AWS_CONFIG_FILE"] = str(config_path)
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    try:
        response = subprocess.run(
            ["aws", "sts", "get-caller-identity", "--profile", profile, "--output", "json", "--no-cli-pager"],
            env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        if response.returncode:
            return {"status": "SSO_CALL_FAILED", "sso_verified": False}
        status = check_identity(json.loads(response.stdout), account, role_name)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {"status": "SSO_CALL_UNAVAILABLE", "sso_verified": False}
    return {"status": status, "sso_verified": status == "SSO_SESSION_OBSERVED",
            "scope": "current_sts_identity_only", "eks_rke2_authorization_verified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    account = os.environ.get("BRAVO_EXPECTED_AWS_ACCOUNT", "")
    if not re.fullmatch(r"[0-9]{12}", account):
        parser.error("Set BRAVO_EXPECTED_AWS_ACCOUNT from reviewed private deployment inputs.")
    result = run(args.profile, account, Path(os.environ.get("AWS_CONFIG_FILE", str(Path.home() / ".aws/config"))))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["sso_verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
