# T46 목적지 큐 서버 후보 비교

ADR-0046 4절의 시험 7개를 후보마다 같은 방식으로 돌린다. 앱처럼 boto3에 주소를 넘기지 않고 `AWS_ENDPOINT_URL_SQS`만 바꾼다. 로컬 Docker 전용이며 팀 환경 · 클라우드를 쓰지 않는다.

```sh
docker compose up -d
uv run --with boto3 python compare.py --candidate elasticmq --endpoint http://127.0.0.1:19324
uv run --with boto3 python compare.py --candidate goaws --endpoint http://127.0.0.1:14100
docker compose down -v
```

## 2026-11-07 결과

boto3 1.43.126, SQS JSON 프로토콜. 이미지는 `compose.yaml`의 digest.

| 시험 | ElasticMQ 1.7.1 (Apache-2.0) | GoAWS (MIT) |
|---|---|---|
| 1 보내기 · 받기 · 지우기 · 속성 | PASS | PASS (큐 주소 호스트가 `us-east-1.goaws.com`) |
| 2 다시 꺼낼 수 있게 되는 시간 뒤 재전달 | PASS (수신 횟수 2) | **FAIL** (재전달은 되나 수신 횟수가 1에 머묾) |
| 3 재시도 한도 뒤 실패 보관 큐 이동 | PASS | PASS |
| 4 재시작 뒤 큐 · 메시지 보존 | PASS (H2 파일 저장 설정) | **FAIL** (재시작 뒤 큐 없음) |
| 5 틀린 자격 · 서명 없는 요청 거부 | FAIL (둘 다 받음) | FAIL (둘 다 받음) |
| 6 TLS 수신 | FAIL (README에 TLS 설정 없음, 실행 시험은 안 함) | FAIL (README에 TLS 설정 없음, 실행 시험은 안 함) |
| 7 메모리 (시험 직후) | 26.5MiB | 13.2MiB |

삭제 확인은 가시성 시간이 지난 뒤 다시 받아 봐서 비어 있을 때만 PASS다(2026-11-08 보강, 두 후보 재실행 결과 같음).

제외: SmoothMQ는 공식 이미지가 없고 AGPL-3.0이며 2025-02 이후 갱신이 없다.

5 · 6은 두 후보 모두 자체 기능으로 충족하지 못한다. 앞단 보완(NetworkPolicy로 허용 Pod만 접속, TLS 종단 프록시)으로 원본 SQS와 같은 시험을 통과하는지는 목적지 클러스터에서 확인한다(T46 spec 완성 기준 10).
