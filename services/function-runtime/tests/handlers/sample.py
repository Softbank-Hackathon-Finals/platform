"""시험용 핸들러. AWS Lambda용으로 쓴 그대로의 형태다."""

import json
import os
import time


def ok(event, context):
    out = os.environ["RESULT_FILE"]
    with open(out, "a", encoding="utf-8") as f:
        for r in event["Records"]:
            f.write(json.dumps({"key": json.loads(r["body"])["idempotency_key"],
                                "receive_count": r["attributes"]["ApproximateReceiveCount"],
                                "remaining_ms": context.get_remaining_time_in_millis(),
                                "request_id": context.aws_request_id,
                                "function": os.environ["AWS_LAMBDA_FUNCTION_NAME"]}) + "\n")
    print("handled", len(event["Records"]))


def boom(event, context):
    raise ValueError("bad message")


def slow(event, context):
    time.sleep(30)


def leak(event, context):
    return {"secret_seen": "ADMIN_TOKEN" in os.environ}
