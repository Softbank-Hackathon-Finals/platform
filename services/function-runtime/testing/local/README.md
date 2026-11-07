# T46 로컬 증명

ElasticMQ · Postgres · 호환 런타임을 Compose로 띄워 T46 spec 완성 기준 8 · 9 · 12를 확인한다. 로컬 Docker 전용이며 팀 환경 · 클라우드를 쓰지 않는다. 값은 모두 시험용 고정값이다.

```sh
docker compose build runtime-base && docker compose build function && docker compose up -d
uv run --project ../.. --with "psycopg[binary]" python prove.py
docker compose down -v
```

- 핸들러 `function/pii_check.py`는 AWS Lambda용으로 쓴 그대로의 메시지 개인정보 검사다. 실행기는 이 파일을 고치지 않고 부른다.
- 송신 쪽은 `boto3.client("sqs")`와 AWS 형식 큐 주소(`https://sqs.ap-northeast-1.amazonaws.com/000000000000/...`)를 그대로 쓰고, `AWS_ENDPOINT_URL_SQS`만 바꾼다.
- 함수 이미지는 런타임 베이스 이미지(`../../Dockerfile`) 위에 핸들러와 의존성만 얹는다(ADR-0041).

## 2026-11-07 결과 (새 환경에서 2회, 모두 PASS)

| 확인 | 기준 | 결과 |
|---|---|---|
| FIFO · 필터 설정, 함수 제한 시간보다 짧은 가시성 시간은 시작 전에 거부 | 12 | 종료 코드 2, 정상 설정은 0 |
| 고치지 않은 송신 코드 · 핸들러로 40건 처리, 개인정보 판정 일치 | 8 | 보낸 키 집합 = 결과 키 집합, 판정 불일치 0 |
| 같은 업무 이벤트 5건 재송신 | 8 | 결과 행 그대로 40, 큐 비워짐 |
| 200건 처리 중 실행기 강제 종료 뒤 재시작 | 8 | 종료 시점 139건 · 142건 처리, 재시작 뒤 240건, 빠짐 0 · 중복 0 |
| 계속 실패하는 메시지(본문 없는 키) | 9 | 3회 실패 뒤 실패 보관 큐로 이동(수신 횟수 4), 결과 행 없음 |
| 끝난 뒤 큐 | — | 대기 0 · 처리 중 0 |

AWS 형식 큐 주소를 그대로 둔 채 `AWS_ENDPOINT_URL_SQS`만 바꿔도 동작한다(spec 7절 확인 항목).
