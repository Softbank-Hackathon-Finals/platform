# T10 창 저장 호스트 포트

T18이 만든 창의 형식·시간·통계 검사는 [T36 관측 계약](../observability/README.md)과 `bravo_observability.intake.accept`가 담당한다. T10은 검증된 생산자와 현재 계획의 관계를 확인하고, 기존 계획별 트랜잭션에서 창을 저장한다. 창 수신은 상태 전이·SHIFT 판정·실행 승인을 의미하지 않는다.

현재 구현은 `services/platform-events/src/bravo_platform_events/window_store.py`다. T36 [#168](https://github.com/Softbank-Hackathon-Finals/platform/pull/168)의 커밋 `020c6c78bdc6133ab0ee15e2a990bb9527b3182f`를 `pyproject.toml`과 `uv.lock`에 고정해 소비한다. 해당 PR은 이 문서 작성 시 검토 중이다. 원본을 복사하거나 T36의 임계값을 변경하지 않는다. upstream 변경은 자동 추종하지 않으며, 채택한 커밋을 바꿀 때 소비 시험을 다시 수행한다.

## 호스트가 연결할 것

| 호출 | 호스트가 보장할 입력 | T10이 같은 트랜잭션에서 확인·저장할 것 |
|---|---|---|
| `set_binding` | 인증·인가를 마친 관리 작업과 불변 참조 `administration_ref`; 검증된 생산자 매핑 | 계획·tenant, 이전 binding revision, 만료, 현재 계획 version/state/digest; append-only 관리 영수증 |
| `receive` | 전용 생산자 자격 검증 뒤 만든 `ProducerIdentity` | 잠금을 얻은 뒤의 시각, 최신 권한·회수·credential ID, 현재 계획 결속, T36 검사와 원자 저장 |
| `latest` | 기존 IdP에서 검증된 운영자 principal | 기존 `OperatorAccess.check_read`의 계획별 조회 권한 |
| `latest_in_transaction` | 계획·상태·권한을 확인한 내부 T17/T10 호출자의 기존 트랜잭션 | 같은 DB snapshot의 최신 창 읽기 |

`ProducerIdentity`는 인증기 자체가 아니다. 요청 JSON, tenant 헤더, 콘솔 토큰을 이 객체로 바로 바꾸면 안 된다. `credential_id`는 호스트가 관리하는 비밀이 아닌 식별자이며 토큰 원문을 저장하지 않는다. `set_binding`은 HTTP 가입 경로가 아니고 `administration_ref` 문자열만으로 관리자 권한이 생기지 않는다.

기본 binding은 없다. 활성화한 binding은 해당 계획의 version/state/digest에 결속한다. 계획이 바뀌면 이전 생산자 자격만으로 다음 단계에 계속 쓰지 못한다. 호스트가 새 관측 단계의 권한을 확인하고 revision을 올려 다시 결속해야 한다. 회수·교체·계획 변경을 쓰는 모든 호출자는 기존 계획 잠금을 공유해야 한다. 과거 활성화 명령의 재시도는 원래 영수증을 반환하며 이후 회수를 되돌리지 않는다.

`receive`는 권한 검사 뒤에 별도 연결을 열지 않는다. T36의 `WindowStore`를 이미 잠근 연결에 주입하고, commit을 마친 뒤에만 수신 결과를 반환한다. commit 응답이 유실되면 `StorageUnavailable`이며 자동 재전송하지 않는다. 호출자가 같은 창을 다시 보내면 이미 commit한 경우 T36의 중복 거부가 나온다. 이를 새 창 저장 성공으로 바꾸지 않는다.

창의 시각은 UTC 마이크로초로 비교한다. T36의 보존 기간보다 오래된 창만 제거하고 최신 창은 남겨 과거 창이 다시 새 입력으로 들어오는 것을 막는다. 남아 있는 최신 창도 시간이 지나면 T36에서 STALE/UNKNOWN이다. 판정할 때 state·weight·관측 시작 시점의 결속은 소비자가 별도로 확인한다.

## T7 DB 적용 순서와 권한

기존 [T10 DB 접속 계약](T10-postgres-storage-v1-proposal.md)과 [T7 접근 경계](../adr/0012-platform-data-access-boundary.md)를 따른다. DSN·CA·자격은 기존 호스트 Secret 주입 경로로 받는다. 생산자에게 DB 접속 권한을 주지 않는다.

1. v1/v2를 읽을 수 있는 소비 코드로 먼저 교체한다. 이 릴리스의 기존 기능은 v1에서도 동작하지만 창 저장은 v2가 없으면 거부된다. v1만 지원하는 이전 코드는 v2 적용 뒤 저장소 초기화를 거부하므로 이전 소비자를 정리한 뒤 진행한다.
2. 별도 migration 로그인으로 `sql/0002_sli_windows.sql`을 한 번 적용한다. DB 버전 검사·새 테이블·권한·버전 갱신은 하나의 트랜잭션이다. 중간 실패는 v1을 보존한다. bootstrap이나 0001을 다시 실행하지 않는다.
3. runtime 로그인으로 창 저장·조회·보존 삭제와 DDL/UPDATE 거부를 확인한다. 기존 `plan_commands`·`events`의 삭제 권한은 추가하지 않는다.
4. 그 뒤에만 승인된 호스트에서 생산자 binding을 만들고 수신을 연결한다. 운영 데이터가 생긴 v2를 이전 코드로 바로 되돌리지 않는다. migration 실패는 롤백되고, 성공 뒤 운영 복구는 T7의 별도 복구 절차를 따른다.

| 역할 | 추가 권한 |
|---|---|
| `bravo_platform_migrator` | 기존 migration 경계에서 `sli_windows` 생성·버전 2 적용 |
| `bravo_platform_runtime` | 새 `sli_windows`에만 SELECT·INSERT·DELETE; UPDATE·DDL 금지 |
| T18 생산자 | 검증된 HTTP 호스트 호출만. SQL 자격 없음 |
| Collector / Executor | 이번 변경으로 새 DB 권한 없음 |

이번 PR은 운영 DB에 migration을 적용하지 않는다. SQLite 참조 저장소는 자체 초기화 시 창 테이블을 만들며, PostgreSQL 호스트가 DDL을 자동 실행하는 방식으로 바꾸지 않는다.

## T18 접속 인계

[T18 실행 위치 스레드](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793721617426529)에 맞춰 창 POST·전용 Bearer·Tailscale 생산자 ACL을 유지한다. 제안 포트 30981은 배포 완료된 수신 주소가 아니다. 실제 생산자 EC2/태그·수신 주소·전용 Secret·제한된 DB egress는 T7/T18/T36과 인계 후 연결한다. 현재 T36 HTTP 호스트는 이 포트에 연결되지 않았고, 이 PR은 공개 HTTP 경로나 토큰 검증기를 추가하지 않는다. 연결 전 생산자는 기존 `windows.jsonl` 경로를 유지한다.

## 검증

```sh
cd services/platform-events
uv sync --locked --all-extras
uv run ruff check .
uv run pytest -q
```

`tests/test_window_store.py`는 SQLite와 실제 PostgreSQL에서 권한 회수·계획 변경·만료의 잠금 대기, 동시 중복/겹침, 보존, 저장 실패, 응답 유실, migration 롤백과 권한 거부를 시험한다. 클라우드 검증은 기존 AWS 호스트의 별도 PostgreSQL 18.4 컨테이너와 합성 데이터로 수행한다. [검증 기록](../evidence/t10-window-store-2026-11-04/README.md)은 운영 CNPG 적용이나 T18 HTTP E2E와 구분한다.
