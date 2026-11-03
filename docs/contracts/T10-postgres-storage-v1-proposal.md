# T10 PostgreSQL 저장 계약 v1 — 제안

상태: **로컬 구현과 실제 임시 DB 검증 제안**. [ADR-0009](../adr/0009-postgres-state-adapter.md), T10 #19/T7 #12 관련이다. 운영 플랫폼 DB 엔진·배포 방식·역할 배정을 확정하지 않는다. 기존 [SSE](T10-events-v1-proposal.md)와 [계획 snapshot](T10-plan-snapshot-v1-proposal.md)의 공개 계약을 그대로 사용한다.

## 호스트가 연결하는 경계

`services/platform-events`의 `PostgresPlanStore(conninfo)`는 `PlanStore`와 같은 Python 경계다. `create_plan_app`에 주입하면 기존 계획 조회와 SSE가 그 DB를 읽는다. SQLite 구현·파일은 그대로 남으며 자동 데이터 이관이나 실행 중 backend 전환은 없다. 추가 설치 의존성은 `bravo-platform-events[postgres]`의 psycopg 3이고, PostgreSQL을 사용하지 않는 SQLite 호스트는 이 extra가 필요 없다.

호스트가 검증한 principal과 저장된 불변 tenant를 대조하는 기존 정책을 그대로 적용한다. DB runtime 계정은 **모든 tenant를 취급하는 신뢰된 호스트 계정**이다. tenant별 DB 역할·RLS 격리가 아니므로 이 계정/conninfo를 사용자·collector·Executor·LLM 도구에 주면 안 된다. collector는 기존 `CollectionProof` 입력 경계로 검증된 증거만 전달하며 직접 테이블을 쓰지 않는다. 승인·T5 정본 검증·추가 상태·T33 저장소 이식은 이번 범위 밖이다.

## 공통 동작과 PostgreSQL 원자성

| 경계 | 보존되는 동작 | PostgreSQL 구현 |
|---|---|---|
| `register_plan` | 불변 spec/tenant, 최초 COLLECT·버전 1·이벤트 1이 전부 기록되거나 전부 취소 | 기존 행이 없어도 적용되는 plan별 transaction advisory lock |
| `complete_collection` | scope/digest·상태·expected version 검증, 상태·이벤트·명령 영수증 원자 저장, 같은 명령 재시도는 원래 영수증 | 같은 lock 뒤 READ COMMITTED, 동일 transaction 내 세 저장 |
| `append` | plan별 순번, 같은 producer key/input 재생, 다른 입력 거부, 관리 상태 우회 거부 | 같은 plan lock, transaction 안의 counter 갱신; 비transactional sequence를 사용하지 않음 |
| `snapshot` | 상태·버전·전체 이벤트 watermark가 같은 조회 시점 | REPEATABLE READ READ ONLY transaction |
| `read_after` | plan/cursor 결속, 연속 순번, 보존 구멍 시 resync_required | stream/cursor/event 조회가 같은 REPEATABLE READ snapshot |

이벤트와 상태의 비즈니스 코드는 두 backend가 공유하고 DB transaction 경계만 바꾼다. 내부의 고정 qmark SQL만 psycopg placeholder로 바꾸며 값은 항상 별도로 바인딩한다. 일반 SQL 변환기나 사용자 SQL 입력 포트가 아니다.

동일 plan을 쓰는 **모든 프로세스는 이 어댑터를 사용**해야 한다. 임의 SQL writer는 advisory lock에 참여하지 않으므로 이 동시성 계약을 보장하지 않는다. lock은 commit/rollback 시 해제되고 다른 plan은 독립적으로 진행한다. plan hash 충돌은 두 plan의 쓰기를 직렬화할 뿐 식별자나 데이터 경계를 합치지 않는다. 조회는 쓰기 완료를 기다리기보다 일관된 과거 snapshot을 반환한다. 스키마는 고정 `bravo_platform`, search path는 `pg_catalog, bravo_platform, pg_temp`이며 runtime은 해당 정본 스키마에 객체를 만들 수 없다.

접속 제한은 5초, lock 대기는 5초, SQL statement는 10초다. 현재 호출마다 연결 하나를 열고 닫으며 연결 풀/자동 retry는 없다. SQL/접속 실패는 DSN·server·DETAIL 없이 `StorageUnavailable("storage_unavailable")`로 전달한다. commit 응답이 끊기면 성공 여부가 불명확할 수 있다. 새 키로 성공을 추정하지 말고 **원래 timestamp/run/input/key로 재시도**해 저장된 영수증을 확인한다. 등록 재시도도 같은 불변 spec을 쓴다.

SSE 헤더 전에는 기존 API 오류 처리, 헤더 후에는 저장 오류로 스트림이 종료되는 기존 동작을 따른다. 헤더 이후 새 HTTP 오류 코드를 보낼 수 없다. 마지막 처리 ID로 재접속해 사전 검사 결과를 확인하며, 지속 장애는 현재 서버 오류다. snapshot watermark는 이후 상태 화면 갱신용이며 삭제된 승인/게이트/감사 이력을 복구하지 않는다.

## migration과 runtime 역할

SQL은 저장소의 `services/platform-events/sql/`에 있다. **전용 빈 DB에 운영자가 검토해 적용할 템플릿**이며 이 PR은 원격 DB에 실행하지 않는다.

1. DB 관리자가 `bootstrap_roles.sql`을 한 번 적용한다. `NOLOGIN` migrator/runtime 그룹 역할과 migrator 소유의 스키마를 만든다. 기존 역할이 있으면 덮어쓰지 않고 실패한다. 전용 DB의 public 스키마 CREATE를 PUBLIC에서 회수한다.
2. 관리자는 서로 다른 login을 만들고 migration login에만 `bravo_platform_migrator`, runtime login에만 `bravo_platform_runtime` membership을 부여한다. login 자격 증명 생성·배포는 이 SQL에 없다. runtime에 migration 역할·소유권·SUPERUSER·CREATEDB·CREATEROLE·BYPASSRLS를 겹쳐 주면 안 된다.
3. migration login으로 `0001_state_event.sql`을 한 번 적용한다. 스키마 생성과 버전 1 기록을 한 transaction으로 처리하며 잘못된 재실행은 실패한다. runtime 연결을 열기 전에 완료한다.
4. runtime login으로 어댑터를 연다. 생성자는 버전 1만 허용하고 DDL을 수행하지 않는다. 배포 전 아래 허용/거부 시험을 같은 login에서 반복해야 한다.

| 역할 | 허용 | 이번 SQL에서 부여하지 않는 권한 |
|---|---|---|
| migrator | 스키마 소유, 명시 migration 및 runtime grant | 클라우드/IAM, 원본 DB, Executor 권한 |
| runtime | 정본 테이블 SELECT, stream/owner/plan/event/receipt INSERT, stream counter와 plan state/version/updated_at UPDATE | 정본 스키마 DDL, tenant/spec UPDATE, event/receipt UPDATE·DELETE, version marker 변경, migration role 승격 |

runtime INSERT는 신뢰된 호스트의 등록/상태 경계를 위한 권한이다. 악성 runtime credential 소유자를 SQL만으로 제한하는 별도 보안 경계가 아니다. DB 관리자·migrator는 데이터 수정이 가능하며 이 저장소는 불변 감사 저장소가 아니다. 보존 삭제 작업은 runtime에 부여하지 않았고 별도 역할/정책이 필요하다. DB 접속/임시 객체·다른 DB/스키마 권한, HBA, TLS, 네트워크, 자격 증명 회전은 운영자 설정이다. 이 fixture가 실제 운영 전체 권한을 검증하지 않는다.

## 시험과 다음 통합 조건

`uv sync --locked` 후 package 디렉터리에서 `uv run pytest -q`를 실행한다. PostgreSQL 서버 패키지의 `pg_config --bindir`에 `initdb`, `pg_ctl`, `postgres`가 있어야 한다. 없으면 실패한다. 시험은 비root 사용자로, 병렬 pytest worker 없이 실행한다. runner의 PostgreSQL 서버 도구를 사용하지만 기존 서버에는 연결하지 않는다.

fixture는 private 임시 디렉터리에 새 cluster를 만든다. Unix socket만 열고 TCP는 비활성화한다. 디렉터리 소유자만 접근 가능한 합성 login/trust fixture이며 운영 HBA 예제가 아니다. migration은 별도 비superuser login으로, 계약 검사는 runtime login으로 수행한다. finally에 자체 서버를 종료하고 임시 파일을 제거한다. 원래 77개 시험을 보존하며 동일 contract를 SQLite/PostgreSQL에 실행한다. PostgreSQL 전용 검사는 immediate shutdown 뒤 WAL 복구·재시도, 권한 거부, 스키마 불일치 및 plan별 독립성을 확인한다.

다음 통합에서는 팀이 엔진/버전·운영 위치·스토리지/백업/복원·secret 주입 경로를 검토해야 한다. 원격 연결에는 검증 가능한 CA/hostname을 갖춘 TLS 정책과 runtime login이 필요하다. 이후 기존 `create_plan_app` 경로에서 실제 접속·동시성·재기동·권한 거부를 다시 측정한다. 원격 네트워크/인증/HA, PgBouncer 모드, 장기 보존/부하, T33 데이터 이식, 기존 SQLite 데이터 migration은 이번 검증에 포함하지 않는다.
