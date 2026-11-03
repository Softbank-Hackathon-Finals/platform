"""Explicit SDK canary for the isolated object store; not demo-chat startup."""
import hashlib
import json
import os
import sys

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

BUCKET = "demo-chat-attachments"
KEY = "t8-development-canary/object-v1.txt"
BODY = b"Team Bravo T8 isolated development object canary v1\n"


def main():
    mode = sys.argv[1]
    if mode not in {"bootstrap", "write", "read"}:
        raise ValueError("bounded probe mode required")
    endpoint = "http://seaweedfs.target-dev.svc.cluster.local:8333"
    credentials = {"aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
                   "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
                   "aws_session_token": None}

    def client(*, unsigned=False, wrong=False):
        return boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
            **{**credentials, **({"aws_secret_access_key": "intentionally-wrong-signature"} if wrong else {})},
            config=Config(signature_version=UNSIGNED if unsigned else "s3v4",
                          s3={"addressing_style": "path"}, connect_timeout=10, read_timeout=20,
                          retries={"max_attempts": 1}, request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"))

    def denied(operation):
        try:
            operation()
            return False
        except ClientError as error:
            return error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403

    s3 = client()
    checks = {}
    if mode == "bootstrap":
        try:
            s3.head_bucket(Bucket=BUCKET)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            s3.create_bucket(Bucket=BUCKET)
        s3.head_bucket(Bucket=BUCKET)
        checks["exact_bucket_ready"] = True
    else:
        if mode == "write":
            # This fixed namespace is owned by the isolated development canary.
            s3.put_object(Bucket=BUCKET, Key=KEY, Body=BODY, ContentType="text/plain")
        response = s3.get_object(Bucket=BUCKET, Key=KEY)
        with response["Body"] as stream:
            value = stream.read(len(BODY) + 1)
        checks["object_length_and_sha256"] = len(value) == len(BODY) and hashlib.sha256(value).digest() == hashlib.sha256(BODY).digest()
        listing = s3.list_objects_v2(Bucket=BUCKET, Prefix=KEY, MaxKeys=2)
        checks["scoped_list"] = any(x.get("Key") == KEY for x in listing.get("Contents", []))
        checks["anonymous_get_denied"] = denied(lambda: client(unsigned=True).get_object(Bucket=BUCKET, Key=KEY))
        checks["wrong_signature_denied"] = denied(lambda: client(wrong=True).get_object(Bucket=BUCKET, Key=KEY))
        checks["out_of_scope_bucket_denied"] = denied(lambda: s3.head_bucket(Bucket="bravo-outside-probe"))
        checks["app_bucket_admin_denied"] = denied(lambda: s3.create_bucket(Bucket=BUCKET))
    print(json.dumps({"mode": mode, "checks": checks, "pass": all(checks.values()),
                      "object_sha256": hashlib.sha256(BODY).hexdigest() if mode != "bootstrap" else None,
                      "app_startup_verified": False, "raw_output_suppressed": True}))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, BotoCoreError, ClientError) as error:
        print(json.dumps({"pass": False, "error_category": type(error).__name__, "raw_output_suppressed": True}))
        raise SystemExit(1) from None
