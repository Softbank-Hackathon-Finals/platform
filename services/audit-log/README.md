# T24 감사 로그: 실행 ID별 해시 체인 + S3 Object Lock

설계 7.4 "감사 로그" 컴포넌트와 8.3 "전부 기록" 가드레일의 저장 부분이다. 새 이벤트를 만들지 않는다. T10 계획 이벤트(6종)와 T11 도구 계층 이벤트를 받아 **실행(run) 단위 해시 체인**으로 묶고, **S3 Object Lock(COMPLIANCE)** 버킷에 한 기록당 한 객체로 보존한다. 삭제 · 단축 · 덮어쓰기가 거부되는지는 같은 패키지의 탐침이 확인한다.

**상태: 시험 132개 통과. 플랫폼 계정에 실제 Object Lock 버킷과 쓰기 · 검색 역할을 만들고([`infra/envs/platform-audit`](../../infra/envs/platform-audit/README.md)), 실제 T10 · T11 생산자 이벤트 기록 · 이어 쓰기 · 재전송 흡수 · 체인 검증 · 삭제 거부 탐침을 확인했다([실측 증빙](../../docs/evidence/t24-audit-live-2026-11-06/README.md)). T10 이벤트 스트림(SSE)을 구독해 버킷에 잇는 `follow` 명령을 실제 T10 앱과 실제 버킷으로 확인했다([follow 실측](../../docs/evidence/t24-follow-live-2026-11-06/README.md)). T10 코드는 바꾸지 않았다. 상시 배포는 아직이다(아래 "남은 일").**

| 파일 | 역할 |
|---|---|
| `record.py` | 기록 v1 스키마, 정규 직렬화, 비밀 패턴 거부, 해시 결속 검증 |
| `ledger.py` | 실행별 체인(`RunLedger`), 체인 검증(`verify_chain`), 저장된 체인 이어쓰기(`resume`) |
| `adapters.py` | T10 이벤트 → 기록, T11 도구 이벤트 → 기록. `kind`는 event_type 또는 도구 사유. 도구 이름은 T11 규칙(`[a-z][a-z0-9_.]{0,63}`, `independent_verifier` 제외)이면 모두 받는다(`sandbox.exec` 등) |
| `sink.py` | `FileSink`(로컬, 덮어쓰기 거부), `ObjectLockSink`(S3, `If-None-Match: *` + COMPLIANCE 보존, readback 필수) |
| `denial.py` | 삭제 · 버전 삭제 · 보존 단축 · 덮어쓰기 · 버킷 정책 · Lock 설정 변경 시도 → 전부 거부여야 PASS |
| `storage.py` | 저장소에 실제로 있는 기록 읽기. S3는 모든 버전 · 삭제 표시까지 읽어 덮어쓰기 · 삭제를 변조로 판정. 계획 하나의 모든 실행을 찾는다(`load_plan_runs`) |
| `reader.py` | 공개 읽기 함수 `read_run`(S3) · `read_local_run`(로컬). 버전 확인 → 체인 검증 → 등록값 대조를 거친 `VerifiedRun`만 돌려주고, 아니면 고정 코드의 `RunRefused`를 낸다(아래 "공개 읽기 함수") |
| `registration.py` | 쓰기 영수증에서 만드는 실행 등록값(`registration_from_receipt`), 형식 검사, 원자적 저장 |
| `follow.py` | `bravo-audit follow`: T10 `GET /v1/plans/{plan_id}/events`(SSE) 구독자. 저장소에서 재개, 엄격한 SSE 파서, 오류별 종료 코드 |
| `cli.py` | `bravo-audit append / follow / verify / probe-denial`. `verify`와 `--resume-from`은 로컬 폴더 · JSON Lines · `s3://버킷/audit/<plan>/<run>`을 받는다 |
| `schemas/audit-record.schema.json` | 기록 v1 JSON Schema |

## T10 이벤트 스트림 구독 (`follow`)

T10 코드를 바꾸지 않고 T24가 T10 SSE의 구독자가 된다. T10이 내보내는 `GET /v1/plans/{plan_id}/events`(재개는 `Last-Event-ID` = T10 `event_id`)를 읽어 기존 어댑터 · 원장 · 저장소를 그대로 거친다.

```sh
# 있는 것까지 기록하고 끝낸다(follow=false). 다시 실행하면 저장소에서 이어 받는다
bravo-audit follow --base-url https://<t10> --plan <plan_id> --sink s3://<bucket>/audit \
  --auth-token-file /run/secrets/t10-read --ca-file /etc/ssl/t10-ca.pem --once
# 상시 구독(follow=true). 끊기면 지수 백오프(최대 --max-backoff 초)로 다시 연결한다
bravo-audit follow --base-url https://<t10> --plan <plan_id> --sink s3://<bucket>/audit --auth-token-file /run/secrets/t10-read
```

- **재개 기준은 저장소다.** 연결할 때마다(첫 시작 · 재시작 · 재연결) 그 계획의 모든 실행 기록을 저장소에서 읽어 체인을 검증하고, 마지막으로 기록된 T10 이벤트 **바로 앞**의 `event_id`를 `Last-Event-ID`로 보낸다. 별도 커서 파일이 없어 버킷과 어긋날 수 없다.
- **같은 이력인지 매번 확인한다.** 마지막으로 기록된 이벤트를 항상 다시 받아, 같은 실행 · 같은 내용(`origin_id`와 payload digest)으로 흡수되는지 본다. 다르면 종료 6(`origin_conflict`)으로 멈추고 아무것도 쓰지 않는다. T10은 커서 행이 있으면 409를 내지 않으므로, T10 저장소가 초기화되거나 다른 백업으로 바뀐 뒤 같은 계획의 새 이력이 저장된 것보다 길면 이 확인 없이는 새 이력의 앞부분을 건너뛰고 뒤에 이어 붙게 된다. 연결마다 이벤트 1건이 다시 오고 흡수된다. 마지막 이벤트가 `data.tool_events`로 나뉜 이벤트였고 기록 도중 죽었다면 같은 재전송으로 나머지가 채워진다.
- **한 번만, 빈틈 없이.** 받은 이벤트는 `sequence`가 바로 다음 번호여야 한다. 기록은 `RunLedger`(재전송 흡수) → 키당 한 번 쓰기(`If-None-Match: *` / 로컬은 원자적 링크)로 남는다. 같은 계획에 follower가 둘 떠도 같은 키에 두 번 쓸 수 없어 늦은 쪽이 멈춘다. 저장소의 T10 순번은 1부터 연속이어야 하고, 한 이벤트가 두 실행에 기록됐거나 기록이 다른 실행 폴더에 있으면 변조로 본다.
- **`--once`의 끝은 한 번 더 연결해 확인한다.** T10에는 스트림 끝 표시가 없다. 헤더를 보낸 뒤 보존 구멍 · 저장소 오류 · 권한 철회로 T10이 스트림을 끊어도 follower에는 정상 종료와 같은 EOF로 온다. 그래서 새 이벤트를 받은 연결이 끝나면 저장소에서 커서를 다시 계산해 다시 연결하고, 새 이벤트 없이 끝난 연결에서만 종료 0을 낸다. 끊긴 원인은 다음 연결에서 T10의 실제 응답(409 · 403 등)으로 드러난다. 상시 모드는 원래 EOF마다 다시 연결한다.
- **SSE는 엄격하게 읽는다.** `id` · `event` · `data`(여러 줄은 LF로 잇기) · `retry`만 받고, 주석(`:`)은 건너뛴다. 모르는 필드, 중복 `id`/`event`, 단독 CR · NUL · 잘못된 UTF-8, 64 KiB를 넘는 줄 · 이벤트, `id`/`event` 줄과 본문의 `event_id`/`event_type` 불일치, 다른 계획의 이벤트, 중복 JSON 키는 거부한다. 연결이 이벤트 중간에 끊기면 그 이벤트는 버린다.
- **자격증명.** `--auth-token-file`의 내용을 `--auth-header`(기본 `Authorization`)에 `--auth-scheme`(기본 `Bearer`)과 함께 보낸다. 명령줄 인자로 받지 않고, 연결할 때마다 다시 읽어 교체를 따른다. `https`만 허용하며 `http`는 루프백 주소에만 쓴다. 리다이렉트는 따라가지 않는다(자격증명을 다른 곳에 보내지 않음).
- **출력.** 표준오류에는 코드가 정한 사유 · 순번 · 개수만 남긴다. 이벤트 본문, T10 오류 본문, 자격증명은 출력하지 않는다. 표준출력의 마지막 줄은 요약 JSON(`records`, `absorbed_redeliveries`, `resumed_after_sequence`, `last_t10_sequence`, 실행별 `chain_digest`). `--receipts`는 쓴 기록마다 영수증 한 줄을 `0600` 파일에 덧붙인다.

| 종료 코드 | 뜻 | 다시 실행하면 |
|---|---|---|
| 0 | `--once`: 끝까지 받음(새 이벤트 없이 끝난 연결로 확인). 상시: SIGTERM/SIGINT로 멈춤 | 이어 받는다 |
| 2 | 설정 오류(인자, 자격증명 파일, TLS 검증 실패, 리다이렉트, 그 밖의 400) | 설정을 고친 뒤 |
| 3 | 저장된 체인이 변조됐거나 앞뒤가 맞지 않음. **연결 · 쓰기 전에 멈춤** | 사람이 조사한다. 자동 재시작 금지 |
| 4 | T10이 401/403/404를 냄(자격증명 · 계획) | 권한을 고친 뒤 |
| 5 | T10이 `409 resync_required` 또는 `400 invalid_cursor`를 냄. 저장소가 가리키는 이벤트부터 T10이 이어줄 수 없음 | 사람이 판단한다. 처음부터 다시 읽으면 체인이 갈라질 수 있어 자동으로 하지 않는다 |
| 6 | 스트림 형식 위반, 모르는 `schema_version`(1.0만 받음), 순번 건너뜀, 어댑터 거부, 같은 `origin_id`의 다른 내용, 비밀 패턴이 든 이벤트 | 생산자를 조사한다 |
| 7 | `--once`에서 T10에 연결하지 못했거나 스트림이 끊김, 다시 요청한 이벤트도 오기 전에 스트림이 끝남, 확인 연결 100번 안에 새 이벤트가 멈추지 않음(상시 모드는 백오프 후 다시 연결) | 이어 받는다 |
| 8 | 저장소가 쓰기를 거부하거나 확인하지 않음(다른 follower가 먼저 씀 포함) | 저장소에서 이어 받는다 |
| 9 | `--once`에서 저장소를 목록 · 읽기 하지 못함(S3 권한 · 네트워크 · 로컬 I/O). 사유에는 예외 이름만 남기고 T10에 연결하지 않음(상시 모드는 백오프 후 다시 읽음) | 이어 받는다. 계속되면 저장소 권한을 확인한다 |

시험(`tests/test_follow.py`)은 실제 T10 `create_app`과 `PlanStore`(`../platform-events/src`, 변경 없음)를 uvicorn으로 띄워 돌린다. 첫 따라잡기, 재시작 뒤 이어 받기, 상시 구독 중 SIGKILL 뒤 재시작, 쓰기 실패 뒤 재시작, 변조 저장소 거부(T10에 연결도 안 함), 403 · 404 · 409, 인증 헤더 전달, 배치 경계(200건) 뒤 보존 구멍과 스트림 중 권한 철회에서 `--once`가 0을 내지 않는 것, T10 저장소를 바꾼 뒤 더 긴 새 이력을 이어 붙이지 않는 것을 실제 T10으로 확인한다. T10 스키마 1.0은 `data`에 `tool_events`를 받지 않으므로(시험으로 고정), `tool_events` 분해와 그 도중 중단, `400 invalid_cursor`, 형식 위반은 같은 형식을 내는 표준 라이브러리 SSE 서버로 확인한다. 실측 스크립트는 `testing/follow_live.py`.

## 공개 읽기 함수 (T21 감사 화면, #270)

콘솔 같은 소비자는 `bravo_audit`의 공개 함수만 쓴다. 내부 함수(`_load_s3_run` 등)는 바뀔 수 있다.

```python
from bravo_audit import read_run, read_local_run, load_registration, RunRefused, VerifiedRun

read_run(client, bucket: str, prefix: str, plan_id: str, run_id: str, *, registration: dict | None = None) -> VerifiedRun
read_local_run(root, plan_id: str, run_id: str, *, prefix: str = "audit", registration: dict | None = None) -> VerifiedRun

# VerifiedRun(plan_id, run_id, records: tuple[dict, ...], record_count: int, chain_digest: str,
#             registered: bool, unregistered_records: int)
# RunRefused.code ∈ READ_CODES, RunRefused.sequence: int | None
```

- `read_run`은 `s3://<bucket>/<prefix>/<plan_id>/<run_id>/`의 **모든 버전과 삭제 표시**를 목록으로 읽는다. 검색 역할(`s3:ListBucketVersions` · `s3:GetObjectVersion`)이면 된다. `prefix`는 sink 루트(보통 `audit`)다.
- 순서대로 확인하고, 하나라도 어긋나면 기록을 하나도 돌려주지 않고 `RunRefused`를 낸다. 오류에는 코드와 순번만 있고 저장소 메시지 · 버킷 이름 · 기록 본문은 없다.

| 코드 | 뜻 |
|---|---|
| `overwritten` | 한 기록 키에 버전이 둘 이상 (덮어쓰기) |
| `delete_marker` | 기록 키에 삭제 표시 |
| `record_outside_its_run` | 기록이 다른 계획 · 실행 · 키(`<sequence:08d>.json`) 아래에 있음 |
| `invalid_record` · `mixed_runs` · `sequence_gap` · `chain_broken` | `verify_chain` 실패 |
| `registration_invalid` | 등록값 형식이 v1이 아님 |
| `registration_mismatch` | 등록값과 저장된 체인이 다름(다른 실행, 등록값보다 짧음, `record_count`번째 기록의 sha256 ≠ `chain_digest`) |
| `run_not_found` | 등록값 없이 읽었는데 기록이 없음 |
| `unreadable_record` · `store_unreadable` · `invalid_identifier` | JSON이 아닌 기록, 저장소 오류(권한 · 네트워크), 잘못된 plan/run ID |

- `read_local_run`은 `FileSink` 폴더용이다. 로컬 폴더는 버전을 남기지 않으므로 `overwritten` · `delete_marker`를 알 수 없다. 시험 · 데모용이고 증빙은 S3로 읽는다.
- CLI: `bravo-audit verify s3://<bucket>/audit/<plan>/<run> --registration reg.json`.

## 실행 등록값 (쓰기 영수증 기반)

버킷 목록만 믿는 읽기는 버킷에 있는 것을 그대로 믿는다. 버킷에 쓸 수 있는 주체가 실행 전체를 **처음부터 다시 해시한 체인**으로 바꿔 두면(새 접두사 · 새 버킷, 각 키 한 버전) 체인 검증과 버전 검사는 모두 통과한다. 그래서 쓰는 쪽이 자기 **쓰기 영수증**(readback까지 확인한 쓰기 결과)으로 등록값을 만들고, 호스트가 그것을 기준으로 넘긴다.

```json
{"schema_version":"1.0","plan_id":"plan-demo","run_id":"run-demo-1","record_count":3,
 "chain_digest":"<마지막으로 쓴 기록의 sha256>","last_sequence":3,"written_at":"2026-11-06T10:00:03Z"}
```

- 만드는 곳: `append --registration-out <파일>`(마지막 영수증으로 한 번), `follow --registration-dir <폴더>`(쓸 때마다 `<run_id>.json`을 원자적으로 교체). 라이브러리는 `registration_from_receipt(plan_id, run_id, receipt)` · `write_registration(path, reg)` · `load_registration(path)`. 파일 권한은 `0600`. `append` 요약 JSON과 영수증 파일에도 같은 값이 `registration`으로 들어간다.
- **버킷 목록에서 만들지 않는다.** 이번 호출에서 새로 쓴 기록이 없으면(재전송만 흡수) 등록값을 새로 쓰지 않는다. 호스트는 가진 것을 계속 쓴다.
- 읽기는 등록값을 **앞부분 기준점**으로 대조한다. 저장된 체인의 `record_count`번째 기록이 있고 그 sha256이 `chain_digest`여야 하며, 돌려주는 기록은 거기까지다. 등록값 뒤에 follower가 더 쓴 기록은 `unregistered_records`로 개수만 알려 준다(새 등록값을 받으면 보인다). 짧아지거나 바뀐 체인은 `registration_mismatch`다.
- **호스트(T21)는 등록값을 감사 버킷 쓰기 주체가 바꿀 수 없는 곳에 둔다.** 감사 버킷 자체나 감사 쓰기 역할이 쓸 수 있는 위치에 두면 기준점이 되지 못한다. 어디에 둘지(콘솔 DB 등)는 T21이 정한다.
- 한계: 등록값은 쓴 주체가 정직했다는 전제의 기준점이다. `--resume-from`으로 이어 쓸 때 앞부분 체인은 저장소에서 읽어 검증한 것이다. `follow`가 기록을 쓴 뒤 등록값 파일 쓰기에 실패하면 종료 8(`registration_not_written`)로 멈추고, 그 기록의 등록값은 다음 쓰기에서 갱신된다(그 전까지 이전 등록값도 앞부분 기준점으로 유효하다).

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
- **T10 연결(11/6)**: T24가 T10 SSE를 구독하는 `follow`로 정했다. T10 코드 변경은 없다. 실제 T10 앱(로컬 저장소)과 실제 버킷으로 첫 기록 · 이어 받기 · 검색 역할 검증을 확인했다([follow 실측](../../docs/evidence/t24-follow-live-2026-11-06/README.md)).
- **남은 일 — follower 배포(인프라 담당)**: T10 읽기 자격증명(계획 이벤트 읽기 권한)을 비밀 저장소에서 파일로 주입하고 `bravo-audit follow`를 상시 실행으로 띄운다(계획마다 하나, 종료 코드 3 · 5 · 6은 자동 재시작하지 않음). follower(또는 T10 런타임) 역할을 `infra/envs/platform-audit`의 `writer_principal_arns`에 추가해야 한다. 지금 쓰기 역할을 맡을 수 있는 주체는 운영자 SSO 역할뿐이다.
- T10이 `data.tool_events`를 내보내려면 T10 스키마가 그 필드를 받아야 한다(현재 1.0은 거부). T24 쪽 분해 · 흡수는 준비돼 있다.
- 콘솔(T21) 표시 형식은 `kind`와 `payload.category`를 그대로 쓰면 된다. 읽기는 `read_run`과 호스트가 보관한 등록값으로 한다(위 "공개 읽기 함수").
- Object Lock은 S3 API 계층의 보호다. 계정 루트의 버킷 삭제, 보존 만료 뒤 삭제는 막지 않는다.
