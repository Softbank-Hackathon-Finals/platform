"""데모 업무: 메시지 개인정보 검사. AWS Lambda용으로 쓴 그대로이며 실행기는 이 파일을 고치지 않고 부른다.

큐 메시지에는 ID만 있다. 본문은 DB에서 읽고, 결과는 업무 멱등 키를 PK로 한 행 하나로 남긴다(T46 spec D7 · D11).
"""

import json
import os
import re

import psycopg

PATTERNS = {
    "rrn": re.compile(r"\b\d{6}-?[1-4]\d{6}\b"),  # 주민등록번호
    "card": re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"),  # 카드번호
    "account": re.compile(r"\b\d{3,6}-\d{2,6}-\d{2,8}\b"),  # 계좌번호
}


def detect(text):
    return sorted(k for k, p in PATTERNS.items() if p.search(text))


def handler(event, context):
    with psycopg.connect(os.environ["DB_DSN"], autocommit=False) as conn:
        for record in event["Records"]:
            key = json.loads(record["body"])["idempotency_key"]
            # 한 이벤트는 짧은 트랜잭션 하나: 읽고, 판정하고, 쓰고 끝낸다.
            with conn.transaction():
                row = conn.execute("SELECT id, body FROM messages WHERE idempotency_key = %s", (key,)).fetchone()
                if row is None:
                    raise LookupError(f"message {key} not found")
                kinds = detect(row[1])
                conn.execute(
                    "INSERT INTO message_checks (idempotency_key, message_id, has_pii, kinds, sqs_message_id)"
                    " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING",
                    (key, row[0], bool(kinds), kinds, record["messageId"]),
                )
