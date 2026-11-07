"""T14 IAM-01.1 canary: write one fixed object, then check what the collector checks. Runs in a Job.

Writes with the bootstrap identity (the client identity cannot create objects outside the app's flow) and
reads it back with the client identity. The unsigned read is only observed, not required: the demo starts
with the bucket in `open-demo` (README "데모용 노출 상태"), where IAM-01.1 is meant to BLOCK until T31 closes
it. Prints only the bucket, key, size, SHA-256 and the outcomes; never a key or a secret.
"""
import hashlib
import json
import os

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

ENDPOINT = "http://seaweedfs.target-dev.svc.cluster.local:8333"
BUCKET = "demo-chat-attachments"
KEY = "attachments/t14-canary.txt"
BODY = b"Team Bravo T14 IAM-01.1 canary v1\n"


def client(prefix=None):
    config = Config(signature_version=UNSIGNED if prefix is None else "s3v4", s3={"addressing_style": "path"},
                    connect_timeout=10, read_timeout=20, retries={"max_attempts": 1},
                    request_checksum_calculation="when_required", response_checksum_validation="when_required")
    keys = {} if prefix is None else {
        "aws_access_key_id": os.environ[f"{prefix}_AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ[f"{prefix}_AWS_SECRET_ACCESS_KEY"],
        "aws_session_token": None,
    }
    return boto3.client("s3", endpoint_url=ENDPOINT, region_name="us-east-1", config=config, **keys)


def main():
    result = {"bucket": BUCKET, "key": KEY, "stage": "bootstrap_put"}
    try:
        client("BOOTSTRAP").put_object(Bucket=BUCKET, Key=KEY, Body=BODY, ContentType="text/plain")
        result["stage"] = "client_get"
        body = client("CLIENT").get_object(Bucket=BUCKET, Key=KEY)["Body"].read()
        result.update(size=len(body), sha256=hashlib.sha256(body).hexdigest(),
                      client_read=body == BODY)
        result["stage"] = "anonymous_get"
        try:
            client().get_object(Bucket=BUCKET, Key=KEY)
            result["anonymous_read"] = "allowed"
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            result["anonymous_read"] = "denied" if status == 403 else f"unknown:{status}"
        result["stage"] = "done"
        result["pass"] = result["client_read"]
    except ClientError as error:
        result.update({"pass": False, "error": error.response.get("Error", {}).get("Code", "ClientError")})
    except Exception as error:  # noqa: BLE001 - one line of evidence, no traceback with values
        result.update({"pass": False, "error": type(error).__name__})
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result.get("pass") else 1)


if __name__ == "__main__":
    main()
