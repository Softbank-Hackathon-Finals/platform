"""T46 목적지 큐 보안 보완 검증(spec 완성 기준 10, ADR-0046 5절). 로컬 k3d 전용.

    uv run --project ../.. python prove_k8s.py          # 클러스터를 만들고 시험한 뒤 지운다
    uv run --project ../.. python prove_k8s.py --keep   # 끝난 뒤 클러스터를 남긴다

원본 SQS에 묻는 질문을 목적지에도 같은 결과로 답하는지 본다.
- 허용된 주체는 TLS로 큐를 쓸 수 있다(양성 대조).
- 허용되지 않은 주체(다른 Pod)는 큐에 닿지 못한다.
- 평문 경로(ElasticMQ 9324 직접, 프록시에 http)는 거부된다.
- CA를 주지 않으면 연결이 실패한다(실제로 TLS 검증을 거친다는 대조).
네트워크 정책을 걸기 전에 같은 시험을 먼저 돌려, 거부가 정책 때문인지(경로 자체가 깨진 것이 아닌지) 확인한다.
남는 위험: 허용된 Pod 안에서는 틀린 자격도 통과한다(ElasticMQ는 서명을 검사하지 않는다).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLUSTER, NS = "t46-queue-sec", "t46-queue-sec"
CONTEXT = f"k3d-{CLUSTER}"
IMAGES = ["t46-function-runtime:local"]
TLS_URL = f"https://queue-tls.{NS}.svc.cluster.local:9443"
PLAIN_PROXY_URL = f"http://queue-tls.{NS}.svc.cluster.local:9443"
DIRECT_URL = f"http://elasticmq.{NS}.svc.cluster.local:9324"

# 앱이 쓰는 그대로의 boto3 호출. 주소 · CA는 환경 변수로만 바꾼다.
PROBE = r"""
import json, os, boto3, botocore
from botocore.config import Config
try:
    sqs = boto3.client("sqs", region_name="ap-northeast-1", aws_access_key_id="t46-local-access",
                       aws_secret_access_key="t46-local-secret",
                       config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}))
    url = sqs.create_queue(QueueName="sec")["QueueUrl"]
    sqs.send_message(QueueUrl=url, MessageBody="x")
    got = sqs.receive_message(QueueUrl=url, WaitTimeSeconds=1).get("Messages", [])
    for m in got:
        sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    print(json.dumps({"ok": bool(got)}))
except Exception as e:
    print(json.dumps({"ok": False, "error": type(e).__name__}))
"""


def run(*args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True, **kw)


def kubectl(*args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return run("kubectl", "--context", CONTEXT, *args, check=check, **kw)


def certs(tmp: str) -> None:
    """시험용 CA와 프록시 서버 인증서. 파일은 임시 디렉터리에만 두고 커밋하지 않는다."""
    san = f"subjectAltName=DNS:queue-tls,DNS:queue-tls.{NS}.svc,DNS:queue-tls.{NS}.svc.cluster.local"
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=t46-test-ca",
        "-keyout", f"{tmp}/ca.key", "-out", f"{tmp}/ca.crt")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=queue-tls", "-addext", san,
        "-keyout", f"{tmp}/tls.key", "-out", f"{tmp}/tls.csr")
    run("openssl", "x509", "-req", "-in", f"{tmp}/tls.csr", "-CA", f"{tmp}/ca.crt", "-CAkey", f"{tmp}/ca.key",
        "-CAcreateserial", "-days", "1", "-copy_extensions", "copy", "-out", f"{tmp}/tls.crt")


def setup() -> None:
    if CLUSTER not in run("k3d", "cluster", "list", "-o", "json").stdout:
        run("k3d", "cluster", "create", CLUSTER, "--wait", "--timeout", "180s")
    for image in IMAGES:
        run("k3d", "image", "import", "-c", CLUSTER, image)
    kubectl("apply", "-f", f"{HERE}/manifests.yaml")
    with tempfile.TemporaryDirectory() as tmp:
        certs(tmp)
        for name, files in (("queue-tls", [f"--cert={tmp}/tls.crt", f"--key={tmp}/tls.key"]),):
            kubectl("-n", NS, "delete", "secret", name, check=False)
            kubectl("-n", NS, "create", "secret", "tls", name, *files)
        kubectl("-n", NS, "delete", "secret", "queue-ca", check=False)
        kubectl("-n", NS, "create", "secret", "generic", "queue-ca", f"--from-file=ca.crt={tmp}/ca.crt")
    kubectl("-n", NS, "rollout", "restart", "deployment/queue-tls")
    for target in ("deployment/elasticmq", "deployment/queue-tls"):
        kubectl("-n", NS, "rollout", "status", target, "--timeout=180s")
    kubectl("-n", NS, "wait", "--for=condition=Ready", "pod/client", "pod/other", "--timeout=180s")


def probe(pod: str, endpoint: str, ca: bool) -> dict:
    env = [f"AWS_ENDPOINT_URL_SQS={endpoint}"] + (["AWS_CA_BUNDLE=/ca/ca.crt"] if ca else [])
    out = kubectl("-n", NS, "exec", pod, "--", "env", *env, "python", "-c", PROBE, check=False, timeout=60)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "no-output", "stderr": out.stderr[-200:]}


def cases() -> dict[str, dict]:
    return {
        "allowed_client_tls": probe("client", TLS_URL, ca=True),
        "other_pod_tls": probe("other", TLS_URL, ca=True),
        "allowed_client_plaintext_direct": probe("client", DIRECT_URL, ca=False),
        "allowed_client_plaintext_to_proxy": probe("client", PLAIN_PROXY_URL, ca=False),
        "allowed_client_tls_without_ca": probe("client", TLS_URL, ca=False),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--keep", action="store_true")
    a = p.parse_args()
    try:
        setup()
        kubectl("-n", NS, "delete", "networkpolicy", "--all", check=False)
        time.sleep(3)
        before = cases()
        kubectl("apply", "-f", f"{HERE}/policies.yaml")
        time.sleep(5)  # 정책 컨트롤러가 규칙을 적용할 시간
        after = cases()
    finally:
        if not a.keep:
            run("k3d", "cluster", "delete", CLUSTER, check=False)
    # 정책 전: 다른 Pod도 닿아야 거부가 정책 때문임을 보일 수 있다. 직접 평문 경로도 열려 있어야 한다.
    controls = {
        "policy_off_other_pod_reaches": before["other_pod_tls"]["ok"],
        "policy_off_direct_plaintext_reaches": before["allowed_client_plaintext_direct"]["ok"],
    }
    want_after = {"allowed_client_tls": True, "other_pod_tls": False, "allowed_client_plaintext_direct": False,
                  "allowed_client_plaintext_to_proxy": False, "allowed_client_tls_without_ca": False}
    got_after = {k: v["ok"] for k, v in after.items()}
    verdict = "PASS" if all(controls.values()) and got_after == want_after else "FAIL"
    json.dump({"verdict": verdict, "controls_without_policy": controls, "with_policy": after,
               "expected_with_policy": want_after,
               "residual_risk": "허용된 Pod 안에서는 틀린 자격도 통과한다(ElasticMQ 서명 미검사)"},
              sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
