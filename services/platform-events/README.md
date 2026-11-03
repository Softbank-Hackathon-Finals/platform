# T10 이벤트 계약과 로컬 어댑터

**상태: 로컬 제안.** T21 콘솔·T24 감사 로그가 사용할 SSE 계약을 시험하는 모듈이다. `platform-api`가 정해지면 import해서 흡수할 수 있는 구성이고, 별도 운영 서비스·DB·배포 단위를 확정한 것이 아니다. 팀 설계 8.2·8.9, T10 #19를 따른다. 상세 제안은 [SSE 계약](../../docs/contracts/T10-events-v1-proposal.md).

## 구성

- `schemas/event.schema.json`: JSON Schema 2020-12. 버전·이벤트 종류별 payload·결속 필드.
- `contract.py`: 스키마와 계획/순번 일치 검사, UTF-8 SSE 인코딩.
- `store.py`: SQLite를 이용한 로컬 순번·멱등 append·재접속 재생 참조 구현.
- `postgres.py`: 기존 상태/이벤트 로직을 재사용하는 선택형 PostgreSQL 저장 어댑터. [접속·역할·검증 계약](../../docs/contracts/T10-postgres-storage-v1-proposal.md).
- `plans.py`: 같은 DB 트랜잭션의 계획 등록 · COLLECT→ANALYZE · 명령 영수증 · 상태 snapshot.
- `plan_api.py`: 서버 principal/tenant 정책을 공유하는 계획 조회와 기존 SSE 경로. [계획 조회 제안](../../docs/contracts/T10-plan-snapshot-v1-proposal.md).
- `executor.py`: T12 실행 상태의 읽기 전용 소비 어댑터. 응답 유실·식별자 불일치·조회 실패는 UNKNOWN으로 유지하고 원시 오류를 반환하지 않는다. [호출 경계와 후속 통합](../../docs/contracts/T10-cutover-submit-v1-proposal.md#실행-상태를-읽는-계약).
- `api.py`: plan-scoped 읽기 전용 FastAPI 어댑터. 인증·인가 callback이 없으면 모든 요청을 거부한다.
- `examples/events.json`: 5종 이벤트의 합성 fixture. 전체 상태 전이 또는 실제 이전의 증거가 아니다.

## 로컬 검사

```sh
uv sync --locked
uv run ruff check .
uv run pytest -q
```

전체 시험은 PostgreSQL 서버 도구(`pg_config`, `initdb`, `pg_ctl`, `postgres`)도 필요하다. 시험은 임시 Unix socket 전용 DB를 직접 시작하고 종료한다. 기존 서버나 환경 변수의 DB URL을 사용하지 않으며 도구가 없으면 건너뛰지 않고 실패한다. Linux CI의 PostgreSQL 서버 패키지와 macOS PostgreSQL 설치 모두 `pg_config --bindir`가 서버 바이너리를 가리켜야 한다. 기존 SQLite 77개 시험만 확인할 때는 `uv run pytest tests/test_events.py tests/test_plans.py -q`를 사용한다.

이벤트 종류는 `state.changed`, `approval.requested`, `approval.decided`, `gate.completed`, `execution.updated`다. 영속 순번은 계획별이며 실행 재시도에서 `run_id`가 바뀌어도 증가한다. 제공자 재전송은 `(plan_id, producer_key)`와 입력 해시로 구별한다. 같은 키·입력은 동일 이벤트를 반환하고 다른 입력은 거부한다.

게이트 요약은 현재 [T5 계약](../../docs/controls/README.md)의 `gate_id`, `context_digest`, `decision`(`PASS`/`BLOCK`)을 보존한다. 컷오버 승인 결속에도 같은 게이트 ID·평가 결속값이 필요하며 `plan_digest`는 T5 서버 context와 같은 형식이다. 이 필드들은 서버 정본의 전체 결과 조회·대조와 소비 시점 권한 검사를 대체하지 않는다. 그런 검사는 향후 승인 처리기가 담당하며, 이 모듈은 이벤트 형식만 검증한다.

호스팅 API는 자신의 인증된 요청과 계획 접근 정책으로 `create_app(store, authorize=...)`에 **동기 함수**를 넘긴다. 반환값이 정확히 `True`일 때만 허용한다. 데이터 존재 여부·cursor 형식을 조회하기 전에 접근을 검사하고 스트림 중에도 재검사한다. append·stream 등록은 HTTP로 노출하지 않는다. 인가된 소유 모듈만 Python API를 호출해야 한다.

`GET /v1/plans/{id}/events`는 기본적으로 계속 읽는다. `Last-Event-ID: plan-demo:3`이면 3 다음부터 재생한다. `?follow=false`는 현재 꼬리를 따라잡으면 닫는 시험·재생 옵션이다. 재접속 손실/중복 시험은 실제 ASGI HTTP 경로를 사용하며, live tail·인가 철회는 비동기 스트림을 직접 검사한다.

응답 헤더가 나간 뒤 보존 구멍이나 저장소 읽기 오류가 생기면 HTTP 상태를 409로 바꿀 수 없어 스트림이 예외로 종료된다. 소비자는 마지막 처리 ID를 유지해 재접속하고, 새 요청의 사전 검사에서 cursor/보존 구멍 오류를 확인해야 한다. 지속되는 저장소 장애는 현재 서버 오류로 남으며 자동 복구하지 않는다. 연결 종료를 정상 완료로 처리하면 안 된다.

## 구현 범위

스키마 검사는 역사적 승인 기록의 형식을 검증한다. 승인 만료·현재 계획/diff/digest와의 대조, 승인자의 권한, 게이트 통과 여부, 상태 전이 허용 여부는 여기서 결정하지 않는다. 그 판정을 담당하는 코드가 실행 전에 다시 검증해야 한다. SSE를 받았다는 이유로 쓰기나 실행을 열어서는 안 된다.

SQLite 파일은 로컬 참조 저장소이며 S3 Object Lock이나 불변 감사 저장소가 아니다. `PlanStore`는 상태 · 이벤트 · 명령 영수증의 로컬 원자 저장과 계획 상태 snapshot을 추가한다. 이력 전체/삭제된 cursor 복구 API, 운영 outbox/DB, 실제 로그인·역할 정책, 원격 PostgreSQL·운영 다중 인스턴스, 프록시 부하·중단 시험, T21/T24 연결, 팀 채택은 남아 있다. 클라우드·팀 E2E를 수행했다는 뜻도 아니다.

## 계획 조회의 로컬 사용

```python
from bravo_platform_events.plan_api import create_plan_app
from bravo_platform_events.plans import PlanStore

store = PlanStore("local-plans.db")
app = create_plan_app(store)  # 기본 미인증: 모든 요청 거부
```

호스트는 검증한 서버 등록값으로 `PlanSpec`을 만들고 `register_plan`을 호출한다. 신뢰된 수집기에서 얻은 `CollectionProof`에만 `complete_collection`을 허용한다. 실제 사용 시 검증된 `Principal`을 반환하는 동기 resolver를 `resolve_principal`로 제공한다. 요청의 tenant 헤더/`complete` 값을 그대로 신뢰하는 예제는 제공하지 않는다.

조회 JSON의 `version`은 상태 변경 횟수이며 `last_event_id`는 같은 읽기 시점의 **전체 이벤트** 마지막 ID다. 상태 화면 초기화 뒤 그 다음부터 SSE를 적용할 수 있지만, snapshot에 없는 승인/감사 이력까지 복구한 것은 아니다. API는 읽기 전용이며 계획 생성 · 임의 전이 · 승인 · 실행 HTTP 경로를 추가하지 않는다. 최초 상태와 유일한 전이 COLLECT→ANALYZE만 다루며 이후 전이와 T5 정본/승인 결속은 후속 작업이다.

## PostgreSQL 저장 어댑터

`PostgresPlanStore`는 별도 배포 없이 `PlanStore` 대신 같은 호스트에 주입한다. 외부 HTTP 경로·JSON·허용 상태는 바뀌지 않는다. SQLite 파일에서 데이터 자동 이관은 하지 않는다.

```python
import os
from bravo_platform_events.postgres import PostgresPlanStore
from bravo_platform_events.plan_api import create_plan_app

store = PostgresPlanStore(os.environ["BRAVO_PLATFORM_DATABASE_URL"])
app = create_plan_app(store)  # 검증된 principal resolver가 없으면 거부
```

설치에는 `postgres` extra를 사용한다. 접속값은 호스트의 시크릿 주입 경로에서 받고 출력하지 않는다. 어댑터는 DDL을 실행하지 않으며 사전 적용한 스키마 버전 1을 확인한다. 관리자용 `sql/bootstrap_roles.sql`, 별도 migration 계정용 `sql/0001_state_event.sql`, runtime 계정 권한 및 원격 TLS·운영 적용의 남은 조건은 [저장소 제안](../../docs/contracts/T10-postgres-storage-v1-proposal.md)에 있다.

로컬 실제 PostgreSQL에서 서로 다른 어댑터 인스턴스의 동시 등록·전이·재시도·append, 쓰기 실패 롤백, 상태와 watermark의 동일 시점, 강제 DB 재시작/WAL 복구 및 권한 거부를 시험한다. 이 결과는 RKE2/CNPG/PgBouncer 연결, 클라우드 Secret 배포, 운영 복원·HA·용량·부하, T33 저장소 이식을 검증한 것이 아니다.

이미 migration된 실제 DB의 제한된 소비 검증은 `python -m bravo_platform_events.runtime_probe`로 수행한다. 기존 `BRAVO_PLATFORM_DATABASE_URL` 주입 경로를 사용하고 TLS/역할을 먼저 확인한다. 합성 계획 한 개와 비공개 checkpoint를 남기는 `exercise`, 새 프로세스에서 같은 상태·이벤트·영수증을 확인하는 `verify`가 있다. DB 재시작·배포·삭제는 수행하지 않는다. [런타임 검증 절차](../../docs/contracts/T10-postgres-runtime-verification.md)의 자격·접속·관측 경계를 따른다.
## CUTOVER 승인·접수 어댑터

`cutover.CutoverService`와 `cutover_api.create_cutover_app`은 호스트가 검증한 CUTOVER 승인을 기존 상태 DB에 결속하고 Executor POST를 한 번만 전송한다. 재시작·응답 유실 뒤에는 저장된 명령으로 GET 조회를 하며 단계별 SSE를 중복 없이 저장한다. `ExecutorReader`는 계속 GET 전용이고 `ExecutorClient`만 POST를 제공한다.

기본 authority와 인증은 거부한다. 실제 T19 정본·SHIFT·승인자 권한을 검증하는 호스트와 전체 계획 상태 머신은 아직 연결되지 않았다. 합성 시험을 운영 승인으로 사용할 수 없다. [계약·신뢰 경계·복구 조건](../../docs/contracts/T10-cutover-submit-v1-proposal.md)을 먼저 읽는다.
