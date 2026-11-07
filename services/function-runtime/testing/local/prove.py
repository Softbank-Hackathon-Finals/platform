"""T46 로컬 증명: spec 완성 기준 8 · 9 · 12를 Compose 환경에서 확인한다.

    docker compose build runtime-base && docker compose build function && docker compose up -d
    uv run --project ../.. --with "psycopg[binary]" python prove.py
    docker compose down -v

앱 쪽 송신 코드는 AWS에서 쓰던 그대로다(`boto3.client("sqs")` + 실제 AWS 형식 QueueUrl). 주소는
`AWS_ENDPOINT_URL_SQS`로만 바꾼다. 결과는 항목별 PASS / FAIL과 근거 수치를 JSON으로 낸다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

import boto3
import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = {
    "AWS_ENDPOINT_URL_SQS": "http://127.0.0.1:19324",
    "AWS_ACCESS_KEY_ID": "t46-local-access",
    "AWS_SECRET_ACCESS_KEY": "t46-local-secret",
    "AWS_DEFAULT_REGION": "ap-northeast-1",
}
# AWS에서 쓰던 형식의 큐 주소. 계정 ID는 시험용 0이다.
QUEUE_URL = "https://sqs.ap-northeast-1.amazonaws.com/000000000000/message-created"
DLQ_URL = "https://sqs.ap-northeast-1.amazonaws.com/000000000000/message-created-dlq"
DB = "postgresql://postgres:local-only-admin@127.0.0.1:15432/chat"
PII = ["주민번호 900101-1234567 입니다", "카드 1234-5678-9012-3456 로 결제", "계좌 110-123-456789 로 보내 주세요"]


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=HERE, check=True, capture_output=True)


# 앱 쪽 코드: AWS에서 쓰던 그대로 ------------------------------------------------
def post_message(conn, sqs, author: str, body: str, key: uuid.UUID | None = None) -> str:
    key = key or uuid.uuid4()
    conn.execute("INSERT INTO messages (idempotency_key, author, body) VALUES (%s, %s, %s)", (key, author, body))
    conn.commit()
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps({"idempotency_key": str(key)}))
    return str(key)


def resend(sqs, key: str) -> None:
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps({"idempotency_key": key}))


# 관측 ----------------------------------------------------------------------
def checks(conn) -> dict[str, bool]:
    conn.commit()
    return {str(k): v for k, v in conn.execute("SELECT idempotency_key, has_pii FROM message_checks").fetchall()}


def all_checked(conn, expected: dict) -> dict | None:
    got = checks(conn)
    return got if set(expected) <= set(got) else None


def wait_for(fn, timeout_s: float, step: float = 0.5):
    deadline = time.monotonic() + timeout_s
    while True:
        value = fn()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(step)


def queue_counts(sqs, url: str) -> dict[str, int]:
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    return {k: int(attrs[k]) for k in ("ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible")}


def cli_check(config: dict) -> int:
    path = os.path.join(HERE, ".prove-config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    try:
        return subprocess.run(["uv", "run", "--project", os.path.join(HERE, "..", ".."), "bravo-function-runtime",
                               "check", "--config", path], env=os.environ | ENV, capture_output=True).returncode
    finally:
        os.remove(path)


def main() -> int:
    os.environ.update(ENV)
    sqs = boto3.client("sqs")
    conn = psycopg.connect(DB)
    results: dict[str, dict] = {}
    expected: dict[str, bool] = {}

    # 기준 12: 지원하지 않는 설정은 시작 전에 거부
    base = {"function_name": "pii-check", "handler": "pii_check.handler", "queue_name": "message-created", "timeout_s": 5}
    codes = {"fifo": cli_check({"bindings": [base | {"queue_name": "message-created.fifo"}]}),
             "filter": cli_check({"bindings": [base | {"filter_criteria": {"Filters": []}}]}),
             "visibility_shorter_than_timeout": cli_check({"bindings": [base | {"timeout_s": 20}]}),
             "valid": cli_check({"bindings": [base]})}
    results["12_unsupported_rejected"] = {
        "verdict": "PASS" if codes == {"fifo": 2, "filter": 2, "visibility_shorter_than_timeout": 2, "valid": 0} else "FAIL",
        "exit_codes": codes}

    # 기준 8 (기본): 고치지 않은 송신 코드 · 핸들러로 결과 저장, 키 집합이 같다
    for i in range(40):
        body = PII[i % 3] if i % 4 == 0 else f"일반 상담 메시지 {i}"
        expected[post_message(conn, sqs, "user", body)] = i % 4 == 0
    got = wait_for(lambda: all_checked(conn, expected), 60) or checks(conn)
    results["8a_basic"] = {"verdict": "PASS" if got == expected else "FAIL", "sent": len(expected),
                           "checked": len(got), "pii_mismatch": sum(1 for k, v in expected.items() if got.get(k) != v)}

    # 기준 8 (중복): 같은 업무 이벤트를 다시 보내도 결과는 1행
    for key in list(expected)[:5]:
        resend(sqs, key)
    drained = wait_for(lambda: queue_counts(sqs, QUEUE_URL) == {"ApproximateNumberOfMessages": 0,
                                                                "ApproximateNumberOfMessagesNotVisible": 0}, 30)
    got = checks(conn)
    results["8b_duplicate"] = {"verdict": "PASS" if drained and got == expected else "FAIL",
                               "resent": 5, "rows": len(got), "queue_drained": bool(drained)}

    # 기준 8 (중단): 처리 중 실행기를 강제 종료하고 다시 켜도 키마다 정확히 1행
    burst = {post_message(conn, sqs, "user", f"부하 {i}"): False for i in range(200)}
    expected |= burst
    time.sleep(0.5)
    compose("kill", "function")
    before_restart = len(checks(conn))
    compose("start", "function")
    got = wait_for(lambda: all_checked(conn, expected), 90) or checks(conn)
    results["8c_killed_mid_batch"] = {"verdict": "PASS" if got == expected else "FAIL", "sent": len(expected),
                                      "checked_before_restart": before_restart, "checked_after": len(got),
                                      "missing": len(set(expected) - set(got)), "extra": len(set(got) - set(expected))}

    # 기준 9: 계속 실패하는 메시지는 재시도 한도(3회) 뒤 실패 보관 큐로, 결과 행은 없다
    poison = str(uuid.uuid4())
    resend(sqs, poison)
    moved = wait_for(lambda: queue_counts(sqs, DLQ_URL)["ApproximateNumberOfMessages"] >= 1, 60, step=1)
    dlq = sqs.receive_message(QueueUrl=DLQ_URL, MaxNumberOfMessages=10, AttributeNames=["All"]).get("Messages", [])
    in_dlq = [json.loads(m["Body"])["idempotency_key"] for m in dlq]
    results["9_failure_to_dlq"] = {
        "verdict": "PASS" if moved and in_dlq == [poison] and poison not in checks(conn) else "FAIL",
        "dlq_keys_match": in_dlq == [poison],
        "receive_count": dlq[0]["Attributes"].get("ApproximateReceiveCount") if dlq else None}

    results["final_queue"] = {"verdict": "PASS" if queue_counts(sqs, QUEUE_URL) == {
        "ApproximateNumberOfMessages": 0, "ApproximateNumberOfMessagesNotVisible": 0} else "FAIL",
        **queue_counts(sqs, QUEUE_URL)}
    json.dump({"queue_url_form": "aws", "results": results}, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0 if all(r["verdict"] == "PASS" for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
