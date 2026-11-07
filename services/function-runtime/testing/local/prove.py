"""T46 로컬 증명: spec 완성 기준 8 · 9 · 12와 D11을 Compose 환경에서 확인한다.

    docker compose build runtime-base && docker compose build function && docker compose up -d
    uv run --project ../.. --with "psycopg[binary]" python prove.py
    docker compose down -v

앱 쪽 송신 코드는 AWS에서 쓰던 그대로다(`boto3.client("sqs")` + 실제 AWS 형식 QueueUrl). 주소는
`AWS_ENDPOINT_URL_SQS`로만 바꾼다. 결과는 항목별 PASS / FAIL과 근거 수치를 JSON으로 낸다.
각 확인은 실패를 잡을 수 있어야 한다: 조건이 만들어지지 않으면(예: 처리 도중에 끄지 못함) PASS가 아니라 FAIL이다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
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
ADMIN = ("http://127.0.0.1:18080/admin/write-block", "t46-local-admin")
# AWS에서 쓰던 형식의 큐 주소. 계정 ID는 시험용 0이다.
SQS_BASE = "https://sqs.ap-northeast-1.amazonaws.com/000000000000/"
QUEUE_URL, DLQ_URL = SQS_BASE + "message-created", SQS_BASE + "message-created-dlq"
SLOW_URL, SLOW_DLQ_URL = SQS_BASE + "slow-check", SQS_BASE + "slow-check-dlq"
DB = "postgresql://postgres:local-only-admin@127.0.0.1:15432/chat"
PII = ["주민번호 900101-1234567 입니다", "카드 1234-5678-9012-3456 로 결제", "계좌 110-123-456789 로 보내 주세요"]
MAX_RECEIVE = 3  # elasticmq.conf의 재시도 한도


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=HERE, check=True, capture_output=True)


# 앱 쪽 코드: AWS에서 쓰던 그대로 ------------------------------------------------
def post_message(conn, sqs, author: str, body: str) -> str:
    key = uuid.uuid4()
    conn.execute("INSERT INTO messages (idempotency_key, author, body) VALUES (%s, %s, %s)", (key, author, body))
    conn.commit()
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps({"idempotency_key": str(key)}))
    return str(key)


def send(sqs, url: str, key: str) -> None:
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps({"idempotency_key": key}))


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


def empty(sqs, url: str) -> bool:
    return queue_counts(sqs, url) == {"ApproximateNumberOfMessages": 0, "ApproximateNumberOfMessagesNotVisible": 0}


def runtime_status() -> dict:
    req = urllib.request.Request(ADMIN[0], headers={"Authorization": f"Bearer {ADMIN[1]}"})
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read())


def cli_check(config: dict, source: str | None = None) -> int:
    path = os.path.join(HERE, ".prove-config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    cmd = ["uv", "run", "--project", os.path.join(HERE, "..", ".."), "bravo-function-runtime", "check", "--config", path]
    try:
        return subprocess.run(cmd + (["--source", source] if source else []), env=os.environ | ENV,
                              capture_output=True).returncode
    finally:
        os.remove(path)


def dlq_receive_count(sqs, url: str, key: str) -> str | None:
    for m in sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, AttributeNames=["All"]).get("Messages", []):
        if json.loads(m["Body"])["idempotency_key"] == key:
            return m["Attributes"].get("ApproximateReceiveCount")
    return None


# 확인 ----------------------------------------------------------------------
def check_unsupported() -> dict:
    """기준 12: 지원하지 않는 설정과 출발지 실제 설정의 차이는 시작 전에 거부한다."""
    with open(os.path.join(HERE, "runtime.json"), encoding="utf-8") as f:
        config = json.load(f)
    base = config["bindings"][0]
    source = os.path.join(HERE, "source")
    with tempfile.TemporaryDirectory() as bad:
        shutil.copytree(source, bad, dirs_exist_ok=True)
        mapping = os.path.join(bad, "pii-check.mapping.json")
        with open(mapping, encoding="utf-8") as f:
            m = json.load(f)
        m["FilterCriteria"] = {"Filters": [{"Pattern": '{"body": {"kind": ["x"]}}'}]}
        with open(mapping, "w", encoding="utf-8") as f:
            json.dump(m, f)
        codes = {
            "fifo": cli_check({"bindings": [base | {"queue_name": "message-created.fifo"}]}),
            "filter_setting": cli_check({"bindings": [base | {"filter_criteria": {"Filters": []}}]}),
            "visibility_shorter_than_timeout": cli_check({"bindings": [base | {"timeout_s": 20}]}),
            "secret_to_handler": cli_check({"bindings": [base | {"pass_env": ["DB_DSN", "ADMIN_TOKEN"]}]}),
            "source_has_filter": cli_check(config, bad),
            "source_batch_differs": cli_check({"bindings": [base | {"batch_size": 5}, config["bindings"][1]]}, source),
            "valid_with_source": cli_check(config, source),
        }
    want = {k: 2 for k in codes} | {"valid_with_source": 0}
    return {"verdict": "PASS" if codes == want else "FAIL", "exit_codes": codes}


def check_basic(conn, sqs, expected: dict) -> dict:
    """기준 8: 고치지 않은 송신 코드 · 핸들러로 결과 저장, 키 집합과 판정이 같다."""
    for i in range(40):
        body = PII[i % 3] if i % 4 == 0 else f"일반 상담 메시지 {i}"
        expected[post_message(conn, sqs, "user", body)] = i % 4 == 0
    got = wait_for(lambda: all_checked(conn, expected), 60) or checks(conn)
    return {"verdict": "PASS" if got == expected else "FAIL", "sent": len(expected), "checked": len(got),
            "pii_mismatch": sum(1 for k, v in expected.items() if got.get(k) != v)}


def check_duplicate(conn, sqs, expected: dict) -> dict:
    """기준 8: 같은 업무 이벤트를 다시 보내도 결과는 1행이고, 다시 보낸 메시지도 처리돼 큐에서 지워진다."""
    for key in list(expected)[:5]:
        send(sqs, QUEUE_URL, key)
    drained = wait_for(lambda: empty(sqs, QUEUE_URL), 30)
    got = checks(conn)
    return {"verdict": "PASS" if drained and got == expected else "FAIL", "resent": 5, "rows": len(got),
            "queue_drained": bool(drained)}


def check_killed_mid_batch(conn, sqs, expected: dict) -> dict:
    """기준 8: 처리 도중 실행기를 강제 종료하고 다시 켜도 키마다 정확히 1행.

    도중임을 확인한다: 끄는 순간 처리 중 배치가 있고(in_flight >= 1), 아직 처리되지 않은 키가 남아 있어야 한다.
    """
    burst = {post_message(conn, sqs, "user", f"부하 {i}"): False for i in range(600)}
    expected |= burst
    wait_for(lambda: len(set(burst) & set(checks(conn))) >= 30, 30, step=0.05)
    in_flight = runtime_status()["in_flight"]
    compose("kill", "function")
    done_at_kill = len(set(burst) & set(checks(conn)))
    compose("start", "function")
    got = wait_for(lambda: all_checked(conn, expected), 120) or checks(conn)
    interrupted = in_flight >= 1 and done_at_kill < len(burst)
    return {"verdict": "PASS" if interrupted and got == expected else "FAIL",
            "burst": len(burst), "in_flight_at_kill": in_flight, "done_at_kill": done_at_kill,
            "interrupted": interrupted, "checked_after": len(got),
            "missing": len(set(expected) - set(got)), "extra": len(set(got) - set(expected))}


def check_failures_to_dlq(conn, sqs) -> dict:
    """기준 9: 예외와 시간 초과 모두 재시도 한도(3회)를 채운 뒤에만 실패 보관 큐로 가고, 결과 행은 없다."""
    poison, slow = str(uuid.uuid4()), str(uuid.uuid4())
    send(sqs, QUEUE_URL, poison)  # 본문 행이 없는 키: 핸들러가 예외를 낸다
    send(sqs, SLOW_URL, slow)  # 제한 시간을 넘기는 핸들러
    wait_for(lambda: queue_counts(sqs, DLQ_URL)["ApproximateNumberOfMessages"] >= 1
             and queue_counts(sqs, SLOW_DLQ_URL)["ApproximateNumberOfMessages"] >= 1, 90, step=1)
    counts = {"exception": dlq_receive_count(sqs, DLQ_URL, poison), "timeout": dlq_receive_count(sqs, SLOW_DLQ_URL, slow)}
    # 수신 횟수 = 한도만큼 실패한 수신 + 실패 보관 큐로 옮긴 수신. 바로 옮겨졌다면 이 값보다 작다.
    want = str(MAX_RECEIVE + 1)
    no_result = poison not in checks(conn)
    return {"verdict": "PASS" if counts == {"exception": want, "timeout": want} and no_result else "FAIL",
            "receive_count": counts, "expected_receive_count": want, "no_result_row": no_result}


def check_short_transactions(conn, sqs, expected: dict) -> dict:
    """D11: 표 잠금으로 핸들러를 막아도 200ms 넘는 chat_app 트랜잭션이 생기지 않고, 잠금이 풀리면 처리된다."""
    longest = [0.0]
    stop = threading.Event()

    def sample():
        with psycopg.connect(DB, autocommit=True) as watch:
            while not stop.is_set():
                row = watch.execute("SELECT coalesce(max(extract(epoch FROM now() - xact_start)), 0) FROM pg_stat_activity"
                                    " WHERE usename = 'chat_app' AND xact_start IS NOT NULL").fetchone()
                longest[0] = max(longest[0], float(row[0]))
                time.sleep(0.02)

    watcher = threading.Thread(target=sample, daemon=True)
    watcher.start()
    with psycopg.connect(DB) as locker:
        locker.execute("LOCK TABLE message_checks IN EXCLUSIVE MODE")
        keys = {post_message(conn, sqs, "user", f"잠금 중 {i}"): False for i in range(5)}
        time.sleep(3)
        blocked_rows = len(set(keys) & set(checks(conn)))
    expected |= keys
    got = wait_for(lambda: all_checked(conn, expected), 60) or checks(conn)
    stop.set()
    watcher.join()
    ok = blocked_rows == 0 and longest[0] < 0.2 and set(keys) <= set(got)
    return {"verdict": "PASS" if ok else "FAIL", "rows_while_locked": blocked_rows,
            "longest_chat_app_xact_s": round(longest[0], 3), "processed_after_unlock": set(keys) <= set(got)}


def main() -> int:
    os.environ.update(ENV)
    sqs = boto3.client("sqs")
    conn = psycopg.connect(DB)
    expected: dict[str, bool] = {}
    results = {
        "12_unsupported_rejected": check_unsupported(),
        "8a_basic": check_basic(conn, sqs, expected),
        "8b_duplicate": check_duplicate(conn, sqs, expected),
        "8c_killed_mid_batch": check_killed_mid_batch(conn, sqs, expected),
        "9_failures_to_dlq": check_failures_to_dlq(conn, sqs),
        "d11_short_transactions": check_short_transactions(conn, sqs, expected),
    }
    final = queue_counts(sqs, QUEUE_URL)
    results["final_queue"] = {"verdict": "PASS" if empty(sqs, QUEUE_URL) else "FAIL", **final}
    json.dump({"queue_url_form": "aws", "results": results}, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0 if all(r["verdict"] == "PASS" for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
