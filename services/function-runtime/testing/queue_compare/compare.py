"""T46 목적지 큐 서버 후보 비교 (ADR-0042 4절).

후보 하나에 같은 시험 7개를 돌리고 항목별 PASS / FAIL / UNKNOWN을 JSON으로 낸다.
앱이 하는 그대로 boto3에 주소를 넘기지 않고 `AWS_ENDPOINT_URL_SQS`로만 바꾼다.

    uv run --with boto3 python compare.py --candidate elasticmq --endpoint http://127.0.0.1:19324

로컬 시험 전용이다. 자격 값은 실제 키 형식이 아닌 고정값을 쓴다.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable

import boto3
import botocore
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

REGION = "ap-northeast-1"
GOOD = ("t46-local-access", "t46-local-secret")
WRONG = ("t46-wrong-access", "t46-wrong-secret")
COMPOSE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compose.yaml")


def client(endpoint: str, creds: tuple[str, str] | None = GOOD, verify: bool = True):
    os.environ["AWS_ENDPOINT_URL_SQS"] = endpoint
    cfg = Config(retries={"max_attempts": 1}, connect_timeout=3, read_timeout=10)
    if creds is None:
        return boto3.client("sqs", region_name=REGION, verify=verify,
                            config=cfg.merge(Config(signature_version=UNSIGNED)))
    return boto3.client("sqs", region_name=REGION, aws_access_key_id=creds[0],
                        aws_secret_access_key=creds[1], verify=verify, config=cfg)


def queue(sqs, name: str, **attrs: str) -> str:
    return sqs.create_queue(QueueName=name, Attributes=attrs)["QueueUrl"]


def receive(sqs, url: str, wait: int = 1) -> list[dict]:
    return sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=wait,
                               AttributeNames=["All"], MessageAttributeNames=["All"]).get("Messages", [])


def check_api(sqs, run: str) -> dict:
    url = queue(sqs, f"api-{run}")
    same = sqs.get_queue_url(QueueName=f"api-{run}")["QueueUrl"] == url
    body = json.dumps({"idempotency_key": str(uuid.uuid4()), "message_id": 1})
    sent = sqs.send_message(QueueUrl=url, MessageBody=body,
                            MessageAttributes={"kind": {"DataType": "String", "StringValue": "message.created"}})
    got = receive(sqs, url)
    ok = (len(got) == 1 and got[0]["MessageId"] == sent["MessageId"] and got[0]["Body"] == body
          and got[0].get("MessageAttributes", {}).get("kind", {}).get("StringValue") == "message.created"
          and "ApproximateReceiveCount" in got[0].get("Attributes", {}) and "SentTimestamp" in got[0].get("Attributes", {}))
    if got:
        sqs.delete_message(QueueUrl=url, ReceiptHandle=got[0]["ReceiptHandle"])
    left = receive(sqs, url)
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    passed = ok and same and not left and "ApproximateNumberOfMessages" in attrs
    return {"verdict": "PASS" if passed else "FAIL", "queue_url_shape": url.split("/")[2:3],
            "get_queue_url_same": same, "fields_ok": ok, "deleted": not left,
            "approximate_attributes": sorted(k for k in attrs if k.startswith("Approximate"))}


def check_redelivery(sqs, run: str) -> dict:
    url = queue(sqs, f"redeliver-{run}", VisibilityTimeout="2")
    sqs.send_message(QueueUrl=url, MessageBody="x")
    first = receive(sqs, url)
    hidden = receive(sqs, url, wait=0)
    time.sleep(3)
    again = receive(sqs, url)
    passed = (len(first) == 1 and not hidden and len(again) == 1
              and again[0]["MessageId"] == first[0]["MessageId"]
              and again[0]["Attributes"].get("ApproximateReceiveCount") == "2")
    return {"verdict": "PASS" if passed else "FAIL", "hidden_while_in_flight": not hidden,
            "receive_count": again[0]["Attributes"].get("ApproximateReceiveCount") if again else None}


def check_dlq(sqs, run: str) -> dict:
    dlq = queue(sqs, f"dlq-{run}")
    arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"].get("QueueArn")
    url = queue(sqs, f"main-{run}", VisibilityTimeout="1",
                RedrivePolicy=json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": "2"}))
    sqs.send_message(QueueUrl=url, MessageBody="poison")
    receives = 0
    for _ in range(4):
        if receive(sqs, url):
            receives += 1
        time.sleep(1.5)
    moved = receive(sqs, dlq)
    passed = receives == 2 and len(moved) == 1 and moved[0]["Body"] == "poison"
    return {"verdict": "PASS" if passed else "FAIL", "receives_before_move": receives, "in_dlq": len(moved),
            "queue_arn_present": arn is not None}


def check_persistence(sqs, run: str, service: str, endpoint: str) -> dict:
    url = queue(sqs, f"persist-{run}")
    sqs.send_message(QueueUrl=url, MessageBody="survive")
    subprocess.run(["docker", "compose", "-f", COMPOSE, "restart", service], check=True, capture_output=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            client(endpoint).list_queues()
            break
        except (BotoCoreError, ClientError):
            time.sleep(1)
    try:
        sqs2 = client(endpoint)
        url2 = sqs2.get_queue_url(QueueName=f"persist-{run}")["QueueUrl"]
        got = receive(sqs2, url2)
        kept = len(got) == 1 and got[0]["Body"] == "survive"
        return {"verdict": "PASS" if kept else "FAIL", "queue_kept": True, "message_kept": kept}
    except ClientError as e:
        return {"verdict": "FAIL", "queue_kept": False, "error": e.response["Error"]["Code"]}


def check_auth(endpoint: str, run: str) -> dict:
    out = {}
    for label, creds in (("wrong_credentials", WRONG), ("unsigned", None)):
        try:
            client(endpoint, creds).list_queues()
            out[label] = "accepted"
        except ClientError as e:
            out[label] = f"rejected:{e.response['Error']['Code']}"
        except BotoCoreError as e:
            out[label] = f"error:{type(e).__name__}"
    passed = all(v.startswith("rejected") for v in out.values())
    return {"verdict": "PASS" if passed else "FAIL", **out}


def check_tls(https_endpoint: str | None) -> dict:
    if not https_endpoint:
        return {"verdict": "FAIL", "native_tls": False, "note": "후보가 TLS 수신 설정을 제공하지 않음(앞단 프록시 필요)"}
    try:
        client(https_endpoint, verify=False).list_queues()
        return {"verdict": "PASS", "native_tls": True}
    except (BotoCoreError, ClientError) as e:
        return {"verdict": "FAIL", "native_tls": False, "error": type(e).__name__}


def check_resources(service: str) -> dict:
    cid = subprocess.run(["docker", "compose", "-f", COMPOSE, "ps", "-q", service],
                         capture_output=True, text=True, check=False).stdout.strip()
    stats = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", cid],
                           capture_output=True, text=True, check=False).stdout.strip()
    image = subprocess.run(["docker", "inspect", "--format", "{{.Image}}", cid],
                           capture_output=True, text=True, check=False).stdout.strip()
    return {"verdict": "PASS" if stats else "UNKNOWN", "memory": stats, "image_id": image}


def guarded(fn: Callable[[], dict]) -> dict:
    try:
        return fn()
    except (BotoCoreError, ClientError, subprocess.CalledProcessError) as e:
        return {"verdict": "UNKNOWN", "error": f"{type(e).__name__}: {e}"[:300]}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--candidate", required=True, help="compose 서비스 이름")
    p.add_argument("--endpoint", required=True)
    p.add_argument("--https-endpoint")
    a = p.parse_args()
    run = uuid.uuid4().hex[:8]
    sqs = client(a.endpoint)
    result = {
        "candidate": a.candidate, "run": run, "boto3": boto3.__version__, "botocore": botocore.__version__,
        "protocol": sqs.meta.service_model.protocol,
        "checks": {
            "1_api": guarded(lambda: check_api(sqs, run)),
            "2_redelivery": guarded(lambda: check_redelivery(sqs, run)),
            "3_dlq": guarded(lambda: check_dlq(sqs, run)),
            "4_persistence": guarded(lambda: check_persistence(sqs, run, a.candidate, a.endpoint)),
            "5_auth": guarded(lambda: check_auth(a.endpoint, run)),
            "6_tls": guarded(lambda: check_tls(a.https_endpoint)),
            "7_resources": guarded(lambda: check_resources(a.candidate)),
        },
    }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0 if all(c["verdict"] == "PASS" for c in result["checks"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
