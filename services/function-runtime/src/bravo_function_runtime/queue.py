"""부품 ①: 큐 연결. 꺼내기 · 지우기 · 속성만 다룬다.

앱과 같이 boto3에 주소를 넘기지 않는다. 목적지 큐 서버 주소는 `AWS_ENDPOINT_URL_SQS`로 정한다.
2단계(공통 인터페이스)로 갈 때 이 부품만 다른 큐 구현으로 바꾼다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Message:
    message_id: str
    receipt_handle: str
    body: str
    attributes: dict[str, str]
    message_attributes: dict[str, dict]
    md5_of_body: str = ""
    md5_of_message_attributes: str = ""


class Queue(Protocol):
    name: str

    def arn(self) -> str: ...

    def attributes(self) -> dict[str, str]: ...

    def receive(self, max_messages: int, wait_s: int) -> list[Message]: ...

    def delete(self, messages: list[Message]) -> None: ...


class DeleteFailed(RuntimeError):
    pass


class SqsQueue:
    def __init__(self, name: str, region: str, client=None):
        if client is None:
            import boto3

            client = boto3.client("sqs", region_name=region)
        self.name = name
        self._sqs = client
        self._url = client.get_queue_url(QueueName=name)["QueueUrl"]

    def attributes(self) -> dict[str, str]:
        return self._sqs.get_queue_attributes(QueueUrl=self._url, AttributeNames=["All"])["Attributes"]

    def arn(self) -> str:
        return self.attributes().get("QueueArn", "")

    def receive(self, max_messages: int, wait_s: int) -> list[Message]:
        out = self._sqs.receive_message(
            QueueUrl=self._url, MaxNumberOfMessages=max_messages, WaitTimeSeconds=wait_s,
            AttributeNames=["All"], MessageAttributeNames=["All"],
        )
        return [
            Message(
                message_id=m["MessageId"], receipt_handle=m["ReceiptHandle"], body=m.get("Body", ""),
                attributes=dict(m.get("Attributes", {})), message_attributes=dict(m.get("MessageAttributes", {})),
                md5_of_body=m.get("MD5OfBody", ""), md5_of_message_attributes=m.get("MD5OfMessageAttributes", ""),
            )
            for m in out.get("Messages", [])
        ]

    def delete(self, messages: list[Message]) -> None:
        for start in range(0, len(messages), 10):
            chunk = messages[start:start + 10]
            out = self._sqs.delete_message_batch(
                QueueUrl=self._url,
                Entries=[{"Id": str(i), "ReceiptHandle": m.receipt_handle} for i, m in enumerate(chunk)],
            )
            if out.get("Failed"):
                # 지우지 못한 메시지는 다시 나온다. 결과 저장이 멱등이라 중복 처리만 생긴다.
                raise DeleteFailed(f"{self.name}: {len(out['Failed'])} message(s) not deleted")
