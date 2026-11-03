# T13 DB 안 무결성 지문 검증 — 2026-11-04

같은 Mac의 PostgreSQL 18.4 컨테이너에서, 10만 행 · 202MB 양쪽 비교가 0.6초에 끝나고 목적지에서 받는 양은 약 7KB였다. 행을 읽는 #134 방식은 같은 실행에서 4.6초에 약 195MB였다.

**로컬 값이다.** TLS, Tailscale, 대역폭 제한, RKE2 위 CNPG, 부하가 모두 없다. 실제 경로의 시간이나 쓰기 정지 전체 시간을 뜻하지 않는다. 컷오버는 실행하지 않았다. Executor 실행 단계에도 아직 연결하지 않았다.

## 측정

[results.json](results.json)이 원본이다. `services/cutover-executor/testing/measure_fingerprint.py`로 만들었다.

- **데이터:** `messages` 10만 행, `attachments` 2,000행, 합계 202MB. 표 구조는 `infra/envs/source-workloads/schema.sql`과 같다.
- **양쪽 설정:** 출발지 `LOCALE C` · UTC, 목적지 `LOCALE C` · Asia/Tokyo.
- **제한:** `statement_timeout` 3초, `lock_timeout` 1초. 표마다 새 연결을 맺는 실제 호출 경로(`DbControl`)로 쟀다.

| 항목 | DB 안 (`db-sql-v1`) | 행 읽기 (#134 `integrity.py`) |
|---|---|---|
| 양쪽 비교 시간, 5회 중앙값 | 0.632초 | 4.554초 |
| 양쪽 비교 시간, 5회 최댓값 | 0.799초 | 5.004초 |
| 목적지에서 받은 양 | 7,437바이트 | 204,913,692바이트 |
| 목적지로 보낸 양 | 5,444바이트 | 49,804바이트 |
| 클라이언트 전송 횟수 | 20회 | 1,054회 |
| 판정 | MATCH | MATCH |

- 쿼리 하나의 시간: `messages` 지문 한쪽 0.289초(5회 중앙값). 3초 제한의 약 10분의 1이다. 실제 환경에서 얼마나 걸리는지는 모른다.
- 앞선 시험의 2KB보다 받는 양이 큰 것은 스키마 지문용 카탈로그 정보와 표마다 맺는 연결이 더해졌기 때문이다. 행 데이터는 포함되지 않는다.
- 비교 대상 #134 코드는 `bcbefb6` 시점의 `integrity.py`다. 받는 양은 대역폭 제한 없는 계수용 프록시로 쟀고, 시간 측정과는 따로 돌렸다.

## 정렬 규칙 실제 조합

| 조합 | 데이터 | 스키마 | 전체 |
|---|---|---|---|
| 같은 데이터, 목적지 DB만 `en_US.utf8` | MATCH | MISMATCH | MISMATCH |
| 같은 정의, glibc 2.41 서버와 2.36 서버 (`18.4`, `18.4-bookworm`) | MATCH | UNKNOWN | UNKNOWN |

## 시험

| 범위 | 결과 |
|---|---|
| `tests/test_db_fingerprint.py`, PG 18.4 | 86 passed |
| `tests/test_db_fingerprint.py`, PG 16 (CI 기본 이미지와 같은 버전) | 86 passed |
| `services/cutover-executor` 전체, PG 18.4 · PG 16 각각 | 170 passed, 39 skipped |
| `develop` 기준 이 브랜치와 #134 `bcbefb6`을 합친 뒤 연결 변경 적용, PG 18.4 | 218 passed, 39 skipped (이때 T13 시험 85개) |

- 건너뛴 39개는 k3d 모의 환경 시험이다. 돌리지 않았으므로 연결된 비교기가 실제 복제 환경에서 끝까지 도는 것은 확인하지 않았다.
- 합친 시험은 임시 폴더에서 했고 #134 브랜치에 올리지 않았다. `integrity.py` · `test_integrity.py`를 지운 상태다. 충돌은 없었다. 같은 시험에서 #134의 비교 시험은 `FixedComparer`나 지운 파일에 있었으므로, 실행 단계가 실제 `DbFingerprintComparer`로 끝까지 도는 경로는 k3d 시험이 맡는다.
- 코드에 결함을 하나씩 넣는 시험 37건을 복사본에서 돌려 모두 시험 실패를 확인했다(1회성, 스크립트는 저장소에 없음). 처음에는 8건을 놓쳐 세션 설정, 스냅샷, 버전 차이, 시퀀스 재확인 시험을 더했다.

## 입력

해시는 `results.json`의 `source_sha256`에 있다. `db_fingerprint.py`와 `measure_fingerprint.py`는 측정 당시 바이트 그대로 커밋했다. 이미지 ID도 같은 파일에 있다.
