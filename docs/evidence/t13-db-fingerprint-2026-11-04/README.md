# T13 DB 안 무결성 지문 검증 — 2026-11-04

같은 Mac의 PostgreSQL 18.4 컨테이너에서, 10만 행 · 202MB 양쪽 비교가 0.9초에 끝나고 목적지에서 받는 양은 약 7KB였다. 행을 읽는 #134 방식은 같은 실행에서 5.3초에 약 195MB였다.

**로컬 값이다.** TLS, Tailscale, 대역폭 제한, RKE2 위 CNPG, 부하가 모두 없다. 실제 경로의 시간이나 쓰기 정지 전체 시간을 뜻하지 않는다. 컷오버는 실행하지 않았다. Executor 실행 단계에도 아직 연결하지 않았다.

## 측정

[results.json](results.json)이 원본이다. `services/cutover-executor/testing/measure_fingerprint.py`로 만들었다.

- **데이터:** `messages` 10만 행, `attachments` 2,000행, 합계 202MB. 표 구조는 `infra/envs/source-workloads/schema.sql`과 같다.
- **양쪽 설정:** 출발지 `LOCALE C` · UTC, 목적지 `LOCALE C` · Asia/Tokyo.
- **제한:** `statement_timeout` 3초, `lock_timeout` 1초. 표마다 새 연결을 맺는 실제 호출 경로(`DbControl`)로 쟀다.

| 항목 | DB 안 (`db-sql-v1`) | 행 읽기 (#134 `integrity.py`) |
|---|---|---|
| 양쪽 비교 시간, 5회 중앙값 | 0.882초 | 5.329초 |
| 양쪽 비교 시간, 5회 최댓값 | 1.019초 | 6.756초 |
| 목적지에서 받은 양 | 7,503바이트 | 204,913,692바이트 |
| 목적지로 보낸 양 | 5,548바이트 | 49,804바이트 |
| 클라이언트 전송 횟수 | 20회 | 1,054회 |
| 판정 | MATCH | MATCH |

- 쿼리 하나의 시간: `messages` 지문 한쪽 0.343초(5회 중앙값). 3초 제한의 약 9분의 1이다. 실제 환경에서 얼마나 걸리는지는 모른다.
- 이번 값은 메이저 확인을 더하기 전 실행(DB 안 0.652초, #134 4.963초)보다 두 방식 모두 느리다. 같은 Mac에서 다른 작업의 부하를 통제하지 않았으므로 작은 차이는 코드 차이로 읽지 않는다.
- 앞선 시험의 2KB보다 받는 양이 큰 것은 스키마 지문용 카탈로그 정보와 표마다 맺는 연결이 더해졌기 때문이다. 행 데이터는 포함되지 않는다.
- 비교 대상 #134 코드는 `bcbefb6` 시점의 `integrity.py`다. 받는 양은 대역폭 제한 없는 계수용 프록시로 쟀고, 시간 측정과는 따로 돌렸다.

## 정렬 규칙 · 버전 실제 조합

| 조합 | 데이터 | 스키마 | 전체 |
|---|---|---|---|
| 같은 데이터, 목적지 DB만 `en_US.utf8` | MATCH | MISMATCH | MISMATCH |
| 같은 정의, glibc 2.41 서버와 2.36 서버 (`18.4`, `18.4-bookworm`) | MATCH | UNKNOWN | UNKNOWN |
| 같은 데이터, PG 18.4와 16.15. 행 수 · PK · 내용 지문은 같음 | UNKNOWN | UNKNOWN | UNKNOWN |
| 같은 데이터, PG 18.4와 18.3 | MATCH | MATCH | MATCH |

PG 18.4와 16.15 조합은 메이저 확인을 넣기 전에는 데이터 MATCH, 스키마 MISMATCH였다. 내용 지문은 그때도 같았다.

## 시험

| 범위 | 결과 |
|---|---|
| `test_db_fingerprint.py` · `test_db_fingerprint_runner.py`, PG 18.4 · PG 16을 번갈아 기준 서버로 | 104 passed |
| `services/cutover-executor` 전체(최신 develop을 합친 뒤), PG 18.4 · PG 16을 번갈아 기준 서버로 | 250 passed, 39 skipped |
| 위에 T12 연결 변경을 임시로 적용, PG 18.4 · PG 16 각각 | 237 passed, 39 skipped |

- **실행 단계 판정:** `test_db_fingerprint_runner.py`는 원격 조작만 가짜로 두고, 실제 두 DB의 비교 결과를 실행기에 넣는다. MATCH는 진행하고, 값 차이 · 스키마 차이 · 미지원 타입은 목적지를 열지 않은 채 영수증을 남긴다.
- **T19 입력:** T13 함수의 실제 출력으로 #141 `baa01f7`의 `assess_readiness` 입력을 채워 돌렸다. 같은 스키마는 PASS, 제약을 추가하거나 PG16과 PG18을 맞붙이면 `SCHEMA_MISMATCH`로 BLOCK이었다(1회성, 스크립트는 저장소에 없음). T19 생산자에 연결한 것은 아니다.
- **T20 방향:** `test_failback_*`이 인자를 바꾼 비교와 시퀀스 보정을 실제 DB로 잰다. T20 코드에 연결한 것은 아니다.
- **건너뛴 39개:** k3d 모의 환경 시험이다. 돌리지 않았다. 그 환경 설정도 `allow_unverified_compare`가 켜져 있어 임시 비교기를 쓴다. 실제 복제 환경에서 이 비교기로 끝까지 돈 적은 없다.
- **연결 변경:** 임시 폴더에서만 적용했다. 내용은 [계약 문서](../../contracts/T13-db-fingerprint-v1.md)의 T12 항목이고 `integrity.py` · `test_integrity.py`를 지운 상태다.
- **결함 주입:** 코드에 결함을 하나씩 넣는 시험 37건을 복사본에서 돌려 모두 시험 실패를 확인했다(1회성, 스크립트는 저장소에 없음). 처음에는 8건을 놓쳐 세션 설정, 스냅샷, 버전 차이, 시퀀스 재확인 시험을 더했다. PR 전 코드 리뷰에서 나온 `sequence_state` 수정은 이 37건을 다시 돌리지 않고, 수정을 되돌린 복사본에서 새 시험 2개가 실패하는 것만 확인했다. 메이저 확인도 37건을 다시 돌리지 않고, 구현 전에 새 시험 6개가 실패하는 것을 확인했다.
- **원인을 못 찾은 실패:** 정리 중 잠시 DB를 쓰는 시험이 PG 18.4 · PG 16 모두 fixture 단계에서 `TypeError`로 실패했다(T13 밖의 시험 포함). 전체 오류 내용을 남기지 못했다. 코드를 바꾸지 않고 바로 다시 돌린 T13 시험 12회와 이후 전체 시험은 모두 통과했다. 환경 문제로 추정하지만 확인하지 못했다.

## 입력

해시는 `results.json`의 `source_sha256`에 있다. `db_fingerprint.py`와 `measure_fingerprint.py`는 측정 당시 바이트 그대로 커밋했다. 이미지 ID도 같은 파일에 있다.
