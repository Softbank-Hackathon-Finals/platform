# T24 감사 로그: 실행 ID별 해시 체인 + S3 Object Lock

설계 7.4 "감사 로그" 컴포넌트와 8.3 "전부 기록" 가드레일의 저장 부분이다. 새 이벤트를 만들지 않는다. T10 계획 이벤트(6종)와 T11 도구 계층 이벤트를 받아 **실행(run) 단위 해시 체인**으로 묶고, **S3 Object Lock(COMPLIANCE)** 버킷에 한 기록당 한 객체로 보존한다. 삭제 · 단축 · 덮어쓰기가 거부되는지는 같은 패키지의 탐침이 확인한다.

**상태: 시험 65개 통과. 플랫폼 계정에 실제 Object Lock 버킷과 쓰기 · 검색 역할을 만들고([`infra/envs/platform-audit`](../../infra/envs/platform-audit/README.md)), 실제 T10 · T11 생산자 이벤트 기록 · 이어 쓰기 · 재전송 흡수 · 체인 검증 · 삭제 거부 탐침을 확인했다([실측 증빙](../../docs/evidence/t24-audit-live-2026-11-06/README.md)). T10 서비스와의 상시 연결은 아직 없다(아래 "남은 일").**

| 파일 | 역할 |
|---|---|
| `record.py` | 기록 v1 스키마, 정규 직렬화, 비밀 패턴 거부, 해시 결속 검증 |
| `ledger.py` | 실행별 체인(`RunLedger`), 체인 검증(`verify_chain`), 저장된 체인 이어쓰기(`resume`) |
| `adapters.py` | T10 이벤트 → 기록, T11 도구 이벤트 → 기록. `kind`는 event_type 또는 도구 사유 |
| `sink.py` | `FileSink`(로컬, 덮어쓰기 거부), `ObjectLockSink`(S3, `If-None-Match: *` + COMPLIANCE 보존, readback 필수) |
| `denial.py` | 삭제 · 버전 삭제 · 보존 단축 · 덮어쓰기 · 버킷 정책 · Lock 설정 변경 시도 → 전부 거부여야 PASS |
| `cli.py` | `bravo-audit append / verify / probe-denial`. `verify`와 `--resume-from`은 로컬 폴더 · JSON Lines · `s3://버킷/audit/<plan>/<run>`을 받는다 |
| `schemas/audit-record.schema.json` | 기록 v1 JSON Schema |

## 기록 형식 (이슈 #28 "실행 ID별 기록 형식 고정")

계약 원문: [`docs/contracts/T24-audit-record-v1-proposal.md`](../../docs/contracts/T24-audit-record-v1-proposal.md).

```json
{"schema_version":"1.0","record_id":"run-demo-1:2","plan_id":"plan-demo","run_id":"run-demo-1",
 "sequence":2,"recorded_at":"2026-11-05T10:00:02Z","source":"agent.tools",
 "kind":"DESTRUCTIVE_ATTEMPT_BLOCKED",
 "payload":{"attempt":1,"tool":"rehearsal.exec","outcome":"DENIED","reason":"DESTRUCTIVE_ATTEMPT_BLOCKED","category":"bucket_delete"},
 "payload_sha256":"…","prev_sha256":"<이전 기록의 sha256>","sha256":"…"}
```

- `sequence`는 실행별 1부터 연속이고 `prev_sha256`이 바로 앞 기록을 가리킨다. 순서를 바꾸거나 하나를 빼거나 내용을 고치면 `verify_chain`이 실패한다.
- 객체 키는 `audit/<plan_id>/<run_id>/<sequence:08d>.json`. 같은 키에 두 번 쓰지 않는다(`If-None-Match: *`).
- `payload`에 자격증명 패턴(AKIA… · `sk-…` · PEM · `user:password@` · `password=…`)이 있거나, `password` · `secret` · `token` · `api_key` · `credential` 류 키 아래에 출처 표식이 아닌 문자열 값이 있으면 기록 자체를 거부한다. 감사 로그는 콘솔과 검토자가 읽는 문서이므로 비밀이 들어가면 안 된다.
- `origin_id`(T10 `event_id`, T11 `attempt+index`)로 재전송을 흡수한다. 같은 identity · 같은 내용은 기록하지 않고, 같은 identity · 다른 내용은 충돌로 거부한다. 재개(`--resume-from`) 뒤에도 같다.
- 한 입력 묶음은 한 실행만 담는다. plan/run이 섞이면 쓰기 전에 거부한다.
- 데모 8.4 장면 "버킷 삭제 시도 차단"은 `source=agent.tools, kind=DESTRUCTIVE_ATTEMPT_BLOCKED, payload.category=bucket_delete` 기록으로 찾는다.

## 삭제 시도 거부 (이슈 #28 "삭제 시도 거부 확인")

`bravo-audit probe-denial --bucket B --key K --version-id V --output readback.json`은 보존된 객체에 대해 여섯 가지를 시도하고 결과 코드만 적는다.

| 시도 | 기대 |
|---|---|
| 현재 버전 삭제 | `AccessDenied` |
| 버전 지정 삭제 | `AccessDenied` (COMPLIANCE 보존 중에는 관리자도 불가) |
| 보존 기간 단축 | `AccessDenied` / `InvalidRequest` |
| 같은 키 덮어쓰기(`If-None-Match: *`) | `412 PreconditionFailed` |
| 버킷 정책 변경 | `AccessDenied` |
| Object Lock 설정 변경 | `AccessDenied` |

하나라도 `ALLOWED`면 `pass=false`다. `DENIED`는 403 · `AccessDenied`뿐이고 400 · 404 · 405는 `UNKNOWN`이다. 보존 상태가 `COMPLIANCE_ACTIVE`로 읽히지 않으면 `pass=false`다(읽지 못하면 `UNKNOWN`, 통과 아님). 출력에 버킷 이름 · 원문 오류 메시지는 넣지 않는다.

## 쓰기 identity 요구사항 (인프라 담당과 맞출 것)

| 항목 | 값 |
|---|---|
| 버킷 | 플랫폼 계정, 생성 시 Object Lock 활성(버전 관리 자동), 기본 보존 COMPLIANCE 1일(데모, 다음 날 정리 가능). 운영은 규제가 정한 기간(보통 수년)을 `--retention-days`와 스택 변수로 준다 |
| 쓰기 역할 | `s3:PutObject`, `s3:PutObjectRetention`(생성 시 보존 지정용), `s3:GetObject`·`s3:GetObjectVersion`·`s3:GetObjectRetention`(readback), `audit/` 접두사 `s3:ListBucket`(저장된 체인 이어 쓰기). 버킷 정책으로 관리자 외 `s3:DeleteObject*`, `s3:PutBucketPolicy`, `s3:PutBucketObjectLockConfiguration` 등 보호 변경 Deny, `s3:BypassGovernanceRetention`은 전원 Deny |
| 검색 역할 | `audit/` 접두사의 `s3:GetObject*`, `s3:ListBucket*` 만 |
| 전송 | TLS 강제(`aws:SecureTransport`), 공개 접근 차단, SSE-KMS |

이 모델은 [ADR-0016](../../docs/adr/0016-target-audit-store.md)(목적지 SeaweedFS 감사 저장소)의 identity 분리와 같다. 플랫폼 쪽만 AWS S3를 쓴다(설계 7.4).

## 사용

```sh
# T10 이벤트(JSON Lines)를 로컬 저장소에 기록
bravo-audit append --source platform.events --input events.jsonl --sink ./store --receipts r1.json
# 같은 실행에 T11 도구 이벤트를 이어 기록 (이미 저장된 기록을 resume 입력으로)
bravo-audit append --source agent.tools --plan plan-demo --input tools.jsonl --sink ./store \
  --receipts r2.json --resume-from stored.jsonl
bravo-audit verify ./store/audit/plan-demo/run-demo-1
# 실제 버킷 (boto3 extra). 쓰기는 쓰기 역할, 검증은 검색 역할로 실행한다
bravo-audit append --source platform.events --input events.jsonl --sink s3://<bucket>/audit --receipts r.json
bravo-audit append --source agent.tools --plan plan-demo --input tools.jsonl --sink s3://<bucket>/audit \
  --receipts r2.json --resume-from s3://<bucket>/audit/plan-demo/run-demo-1
bravo-audit verify s3://<bucket>/audit/plan-demo/run-demo-1
bravo-audit probe-denial --bucket <bucket> --key audit/plan-demo/run-demo-1/00000001.json --version-id <v> --output readback.json
```

## 남은 일 · 한계

- **실측 완료(11/6)**: 실제 버킷 쓰기 · readback · 이어 쓰기 · 재전송 흡수 · 검색 역할 검증 · 삭제 거부 탐침. 입력은 T10 `PlanStore`와 T11 도구 계층이 실제로 만든 이벤트다([증빙](../../docs/evidence/t24-audit-live-2026-11-06/README.md)).
- `data.tool_events`: T10 이벤트에 실린 배열을 원래 순번 그대로 도구 기록으로 나눈다(`expand_platform_event`). 같은 시도를 T11에서 직접 보낸 경우와 `origin_id`가 같아 어느 경로로 와도 한 번만 기록된다. 상태가 `COMPLETE`가 아니거나 다른 실행의 이벤트가 섞이면 거부한다.
- **미연결**: T10 서비스의 상시 기록 경로. 지금은 CLI로 넣는다. 쓰기 역할의 신뢰 주체도 아직 운영자 SSO 역할뿐이라, T10 런타임 역할을 `writer_principal_arns`에 추가해야 한다.
- T10 서비스가 이벤트를 커밋할 때 이 라이브러리를 직접 호출할지, SSE를 구독하는 별도 writer로 둘지는 T10 담당과 정한다(현재는 CLI · 라이브러리만).
- 콘솔(T21) 표시 형식은 `kind`와 `payload.category`를 그대로 쓰면 된다.
- Object Lock은 S3 API 계층의 보호다. 계정 루트의 버킷 삭제, 보존 만료 뒤 삭제는 막지 않는다.
