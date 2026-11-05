# [T24] 감사 기록 v1 제안

상태: **로컬 구현 · 시험 제안, 팀 채택 전**. 기준: `docs/design.html` 7.4 감사 로그, 8.3 가드레일 "전부 기록", 8.4 데모 장면, FR-14. 생산자는 T10(계획 이벤트) · T11(도구 이벤트) · T19(게이트) · T12(Executor), 소비자는 T21 콘솔 · 검토자다. 스키마 원본: [`audit-record.schema.json`](../../services/audit-log/src/bravo_audit/schemas/audit-record.schema.json).

## 기록 한 건

| 필드 | 의미 |
|---|---|
| `schema_version` | `1.0` |
| `record_id` | `<run_id>:<sequence>` |
| `plan_id` · `run_id` | T10 식별자와 같은 형식. 한 체인은 한 실행만 담는다 |
| `sequence` | 실행별 1부터 연속 정수. 타임스탬프로 정렬하지 않는다 |
| `recorded_at` | writer가 기록한 UTC 시각(초). 순서 근거가 아니다 |
| `source` | `platform.events` · `agent.tools` · `gate` · `executor` · `rehearsal` |
| `kind` | T10은 `event_type`, T11은 사유 열거값(`DESTRUCTIVE_ATTEMPT_BLOCKED` 등) |
| `origin_id` | 원 이벤트의 identity. T10은 `t10:<event_id>`, T11은 `t11:<run_id>:attempt<n>:<index>`. 같은 identity가 같은 내용으로 다시 오면 기록하지 않고 흡수하고, 다른 내용이면 충돌로 거부한다(T10 계약의 "T24는 이벤트 ID별 중복을 제거한다") |
| `payload` | 원 이벤트의 내용. 16 KiB 이하, 비밀 패턴 금지(있으면 기록 거부) |
| `payload_sha256` | `payload` 정규 JSON의 SHA-256 |
| `prev_sha256` | 앞 기록의 `sha256`, 첫 기록은 64자리 0 |
| `sha256` | `sha256`을 뺀 나머지 전체의 정규 JSON SHA-256 |

정규 JSON은 키 정렬 · 구분자 `,` `:` · UTF-8이다. 같은 입력을 같은 순서로 넣으면 같은 체인이 나온다.

## 저장과 영수증

- 객체 키 `audit/<plan_id>/<run_id>/<sequence:08d>.json`, 한 키에 한 번만(`If-None-Match: *`).
- 쓰기는 COMPLIANCE 보존을 명시하고 응답의 `VersionId`와 readback(`ObjectLockMode` · `RetainUntilDate`)으로 확인한다. readback은 끌 수 없고, 확인되지 않으면 "기록됨"으로 보고하지 않는다.
- 한 입력 묶음은 한 실행(plan, run)만 담는다. 섞여 있으면 아무것도 쓰지 않고 거부한다.
- `payload`의 문자열 검사와 별도로 **구조 검사**를 한다. `password` · `secret` · `token` · `api_key` · `credential` 류 키 아래의 문자열 값은 출처 표식(`secretKeyRef` · `literal` · `<masked>` 등)만 허용한다. `secret_id` · `secret_arn` · `token_count` · `password_ref`처럼 참조 · 수량을 뜻하는 이름은 **어느 깊이에 있든** 제외한다. 참조 키의 예외는 **문자열 값 하나**에만 적용된다. 참조 키 아래에 객체 · 목록이 오면 상위의 민감 문맥을 그대로 물려받으므로 `password: {password_ref: {value: …}}` 같은 값은 거부되고, 참조 키 옆에 놓인 비참조 문자열도 여전히 거부한다.
- 영수증: `key` · `sha256` · `version_id` · `retain_until` · `retention_mode`. 콘솔은 영수증과 `chain_digest`(마지막 `sha256`)를 실행 화면에 붙인다.

## 검증 규칙 (`verify_chain`)

순서대로 (1) 스키마 · 해시 결속, (2) 한 실행만, (3) `sequence` 연속, (4) `prev_sha256` 연결. 실패 사유는 `invalid_record:<n>` · `mixed_runs:<n>` · `sequence_gap:<n>` · `chain_broken:<n>` 라벨만 낸다.

## 삭제 시도 거부 readback

`probe-denial`은 여섯 시도의 결과 코드(`DENIED` · `PRECONDITION_FAILED` · `ALLOWED` · `UNKNOWN:<code>`)와 보존 상태(`COMPLIANCE_ACTIVE` · `NOT_RETAINED` · `UNKNOWN`)만 기록한다. `DENIED`는 403 · `AccessDenied`뿐이다. 400 · 404 · 405 같은 잘못된 요청 · 미지원 응답은 보호의 증거가 아니므로 `UNKNOWN`이다. PASS는 여섯 시도가 전부 기대값이고 보존이 `COMPLIANCE_ACTIVE`로 확인됐을 때만이다. 버킷 이름 · 원문 오류는 넣지 않는다. 이 readback이 공개 증빙의 원본이다.

## 생산자별 연결

| 생산자 | 넣는 것 | 연결 방식(제안) |
|---|---|---|
| T10 | 계획 이벤트 6종 그대로 | 이벤트 커밋 트랜잭션 뒤 `RunLedger.append` 또는 SSE 구독 writer |
| T11 | `Tools.events` 항목(코드가 정한 열거값만) | 시도 종료 시 일괄 append |
| T19 · T12 | 판정 · 실행 결과 요약 | 각 담당과 필드 합의 후 `source=gate` · `executor` |

미결: T10 writer 위치. 결정(11/6): 보존 기간은 데모 1일(COMPLIANCE), 운영은 규제 기간. 플랫폼 계정 버킷은 T24 담당이 `infra/envs/platform-audit`으로 만든다.
