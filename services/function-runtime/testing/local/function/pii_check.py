"""데모 업무: 메시지 개인정보 검사. AWS Lambda용으로 쓴 그대로이며 실행기는 이 파일을 고치지 않고 부른다.

큐 메시지에는 메시지 행 번호(message_id)만 있다. 본문은 DB에서 읽고, 결과는 message_id를 PK로 한 행 하나로 남긴다
(T46 spec D7 · D11). 일치한 값과 본문은 결과 · 로그에 남기지 않고 종류와 개수만 저장한다.
"""

import json
import os
import re

import psycopg

# 긴 형식부터 찾고, 찾은 부분은 지운 뒤 다음 형식을 찾는다. 카드번호 안의 숫자 묶음이 계좌번호로 또 세지지 않게 한다.
PATTERNS = {
    "card": re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b"),  # 카드번호
    "rrn": re.compile(r"\b\d{6}-?[1-4]\d{6}\b"),  # 주민등록번호
    "account": re.compile(r"\b\d{3,6}-\d{2,6}-\d{2,8}\b"),  # 계좌번호
}


def detect(text):
    """종류별 개수만 돌려준다. 일치한 값은 밖으로 내보내지 않는다."""
    counts = {}
    for kind, pattern in PATTERNS.items():
        text, n = pattern.subn(" ", text)
        if n:
            counts[kind] = n
    return counts


# 한 이벤트의 트랜잭션을 200ms 안에 끝낸다(T46 spec D11, Executor의 긴 트랜잭션 기준과 같다).
# 잠금을 오래 기다리거나 문장이 길어지면 DB가 끊고, 이벤트는 실패로 남아 재전달된다.
SESSION_LIMITS = "-c lock_timeout=50 -c statement_timeout=80 -c idle_in_transaction_session_timeout=150"


def handler(event, context):
    with psycopg.connect(os.environ["DB_DSN"], autocommit=False, options=SESSION_LIMITS) as conn:
        for record in event["Records"]:
            message_id = int(json.loads(record["body"])["message_id"])
            # 한 이벤트는 짧은 트랜잭션 하나: 읽고, 판정하고, 쓰고 끝낸다.
            with conn.transaction():
                row = conn.execute("SELECT body FROM messages WHERE id = %s", (message_id,)).fetchone()
                if row is None:
                    raise LookupError(f"message {message_id} not found")
                counts = detect(row[0])
                conn.execute(
                    "INSERT INTO message_checks (message_id, has_pii, kinds, counts, sqs_message_id)"
                    " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING",
                    (message_id, bool(counts), sorted(counts), json.dumps(counts), record["messageId"]),
                )
