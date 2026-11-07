"""부품 ③: 메시지 형식. 큐 메시지를 AWS Lambda의 SQS 이벤트와 context 값으로 바꾼다.

핸들러는 AWS에서 받던 것과 같은 키를 받는다. 2단계로 갈 때 이 부품만 공통 형식으로 바꾼다.
"""

from __future__ import annotations

import base64
import uuid

from .config import Binding
from .queue import Message

LAMBDA_ATTRIBUTES = ("ApproximateReceiveCount", "SentTimestamp", "SenderId", "ApproximateFirstReceiveTimestamp")


def _message_attribute(value: dict) -> dict:
    binary = value.get("BinaryValue")
    if isinstance(binary, bytes | bytearray):
        binary = base64.b64encode(binary).decode()
    out = {
        "stringListValues": list(value.get("StringListValues", [])),
        "binaryListValues": [base64.b64encode(b).decode() if isinstance(b, bytes | bytearray) else b
                             for b in value.get("BinaryListValues", [])],
        "dataType": value.get("DataType", "String"),
    }
    if "StringValue" in value:
        out["stringValue"] = value["StringValue"]
    if binary is not None:
        out["binaryValue"] = binary
    return out


def record(message: Message, queue_arn: str, region: str) -> dict:
    return {
        "messageId": message.message_id,
        "receiptHandle": message.receipt_handle,
        "body": message.body,
        "attributes": {k: message.attributes[k] for k in LAMBDA_ATTRIBUTES if k in message.attributes},
        "messageAttributes": {k: _message_attribute(v) for k, v in message.message_attributes.items()},
        "md5OfBody": message.md5_of_body,
        **({"md5OfMessageAttributes": message.md5_of_message_attributes} if message.md5_of_message_attributes else {}),
        "eventSource": "aws:sqs",
        "eventSourceARN": queue_arn,
        "awsRegion": region,
    }


def event(messages: list[Message], queue_arn: str, region: str) -> dict:
    return {"Records": [record(m, queue_arn, region) for m in messages]}


def context(binding: Binding, region: str, account_id: str, deadline_ms: int) -> dict:
    """하위 프로세스가 context 객체를 만들 때 쓰는 값. 남은 시간은 deadline으로 계산한다."""
    return {
        "aws_request_id": str(uuid.uuid4()),
        "function_name": binding.function_name,
        "function_version": "$LATEST",
        "invoked_function_arn": f"arn:aws:lambda:{region}:{account_id}:function:{binding.function_name}",
        "memory_limit_in_mb": str(binding.memory_mb),
        "log_group_name": f"/aws/lambda/{binding.function_name}",
        "log_stream_name": "bravo-function-runtime",
        "deadline_ms": deadline_ms,
    }
