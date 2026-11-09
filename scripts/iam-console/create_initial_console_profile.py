"""Receive a Keychain-backed password over stdin; emit metadata only."""

import json
import logging
import resource
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


def create(session, request):
    options = Config(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1})
    if (
        session.client("sts", config=options).get_caller_identity()["Account"]
        != request["expected_account"]
    ):
        raise ValueError("account_boundary_failed")
    iam = session.client("iam", config=options)
    user = iam.get_user(UserName=request["user_name"])["User"]
    tags = {tag["Key"]: tag["Value"] for tag in user.get("Tags", [])}
    if (
        user.get("Path") != "/team-bravo/humans/"
        or tags.get("principal-type") != "human"
        or tags.get("project") != "softbank-finals"
    ):
        raise ValueError("target_not_managed_human")
    try:
        iam.get_login_profile(UserName=request["user_name"])
        return {"console_profile": "existing_preserved", "login_verified": False}
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchEntity":
            raise
    iam.create_login_profile(
        UserName=request["user_name"], Password=request["password"], PasswordResetRequired=True
    )
    profile = iam.get_login_profile(UserName=request["user_name"])["LoginProfile"]
    if profile.get("PasswordResetRequired") is not True:
        raise ValueError("profile_readback_failed")
    return {
        "console_profile": "created",
        "password_reset_required": True,
        "login_verified": False,
        "secret_output": False,
    }


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    logging.disable(logging.CRITICAL)
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps({"sdk_probe": "PASS"}))
        return 0
    try:
        request = json.load(sys.stdin)
        if request == {"transport_probe": True}:
            print(json.dumps({"memory_pipe_probe": "PASS", "aws_calls": 0}))
            return 0
        session = boto3.Session(profile_name=request.get("profile"), region_name="ap-northeast-2")
        result = create(session, request)
    except (BotoCoreError, ClientError, OSError, ValueError, KeyError, TypeError):
        # SDK exception bodies can contain request context. Never serialize them.
        print(json.dumps({"error_code": "sdk_profile_operation_failed"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
