# T10 PostgreSQL 런타임 연결 검증

이 문서는 [PostgreSQL 저장 계약 제안](T10-postgres-storage-v1-proposal.md)의 소비 검증 절차다. 새로운 DB 엔진·namespace·스토리지·역할 운영 정책을 채택하지 않는다. 인프라 담당자가 검토한 별도 플랫폼 DB와 migration을 제공한 뒤 기존 `PostgresPlanStore`로 연결한다. 출발지/목적지 채팅 DB를 검증 대상으로 사용하지 않는다.

## 인프라 담당자가 제공하는 경계

- 전용 플랫폼 DB와 고정 `bravo_platform` 스키마, `schema_version=1` 행 하나. `services/platform-events/sql/bootstrap_roles.sql`과 `0001_state_event.sql`의 검토된 적용 기록.
- 서로 다른 migration/runtime login. runtime은 `bravo_platform_runtime`만 상속하며 DB·스키마·테이블 소유자나 migrator가 아니다. SUPERUSER/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS 및 정본 테이블 DELETE/TRUNCATE/TRIGGER 권한이 없어야 한다. 이 도구는 runtime으로 DDL이나 migration을 하지 않는다.
- 비공개 DNS/포트/DB명, 서버 인증 hostname, CA 배포 경로, runtime secret 참조, 승인된 접속 경로. 자격은 기존 호스트 예제와 같은 `BRAVO_PLATFORM_DATABASE_URL` 환경 변수로 주입하며 명령 인자·Git·출력 파일에 넣지 않는다. `host`, `dbname`, `user`, `sslrootcert`, `sslmode=verify-full`을 명시한다. 터널은 인증할 `host`와 TCP 경로인 `hostaddr`를 구별한다.
- runtime 자격은 신뢰된 플랫폼 host 전용이다. collector·Executor·사용자·모델 도구에 공유하지 않으며 tenant별 SQL/RLS 격리 계정으로 표현하지 않는다.

## 실행

해당 package와 PostgreSQL extra를 고정 버전으로 설치한다. 저장소에서 실행할 때는 `services/platform-events`에서 `uv sync --locked --extra postgres`를 사용한다. 인프라 담당자가 제공한 비공개 자격 환경에서 다음 두 단계만 수행한다.

```sh
python -m bravo_platform_events.runtime_probe exercise --checkpoint "$PRIVATE_CHECKPOINT"
python -m bravo_platform_events.runtime_probe verify --checkpoint "$PRIVATE_CHECKPOINT"
```

`exercise`는 TLS가 실제 활성화됐는지, 로그인/DB명이 명시 입력과 같은지, runtime 역할·테이블 소유권·파괴적 테이블 권한이 계약과 맞는지 먼저 읽기 전용으로 확인한다. 약한 TLS나 잘못된 권한은 합성 계획을 생성하기 전에 실패한다.

그 뒤 새 `pg-probe-<random>` 계획 **하나**를 만들고, 네 연결의 같은 등록·전이 명령 재시도, COLLECT→ANALYZE, 잘못된 이벤트의 transaction rollback, 두 연결의 이벤트 추가·중복 제거, 연속 순번 1~4, snapshot watermark와 다른 계획 cursor 거부를 검증한다. 수집 증거는 합성 입력이며 실제 collector 검증 완료가 아니다. tenant/spec/event/receipt 변경 거부는 합성 계획에만 고정된 UPDATE를 실행해 SQLSTATE 42501을 확인한다. 모든 부정 시험은 성공·실패와 관계없이 rollback한다. 객체 부재나 연결 실패를 권한 거부로 세지 않는다. 임의 SQL, 삭제, DROP/TRUNCATE, 재배포·DB 재시작 기능은 없다.

checkpoint는 첫 DB 쓰기 전에 새 파일로만 생성하며 모드 0600이다. 자격 없이 합성 계획 식별자와 원래 timestamp만 보존한다. 기존 checkpoint를 덮어쓰지 않는다. 실패한 실행에도 합성 자료가 남을 수 있으며, 자동 삭제하지 않는다. 동일 계획을 새 `exercise`로 덮어쓰는 것도 거부한다.

`verify`는 별도 프로세스/연결에서 같은 checkpoint를 사용해 상태·이벤트·원래 명령 영수증을 확인한다. 사라진 상태나 영수증을 재생성하지 않는다. 원래 timestamp/run/key의 명령 재시도가 원래 영수증을 반환하고 snapshot을 바꾸지 않는지 확인한다. 인프라 담당자가 별도로 승인된 DB 재시작/복구를 실행했다면 전후 관측 및 이 verify 결과를 함께 보존한다. 단순 재접속을 DB 재시작·백업 복원으로 표현하지 않는다.

## 결과와 한계

표준 출력은 PASS/FAIL과 고정 오류 코드, 개수·boolean·서버 버전·실행 파일 hash·checkpoint hash 중심 JSON이다. DSN, hostname, 계정명, 비밀번호, 서버 오류 DETAIL을 출력하지 않는다. 성공은 합성 저장 계약의 해당 관측이며 실제 API 인증/배포, T33 이식, 승인·Executor, 불변 감사, HA, 백업 복원이나 T7 전체 완료를 뜻하지 않는다.

로컬 시험은 기존 임시 PostgreSQL fixture를 사용하며, 별도 loopback PostgreSQL의 임시 인증서·SCRAM 계정으로 verify-full 및 잘못된 hostname 거부도 검증한다. 이 로컬 fixture의 임시 CA/자격/HBA는 운영 설정 예제가 아니다. 원격 실행 증빙은 실제 실행 이후 별도로 추가한다.
