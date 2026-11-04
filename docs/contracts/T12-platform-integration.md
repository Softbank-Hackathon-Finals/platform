# T12와 플랫폼·워크로드 연결 계약

상태: 구현 및 검증 제안. 정본은 `docs/design.html` 7.2·7.5·8.6·8.10이다.
실행 소유자는 T12, 배포 소유자는 T6/T30, 플랫폼 소비자는 T10이다.
이 문서는 실행 순서·승인·실패 분기를 바꾸지 않으며 각 이슈의 전체 완료를 뜻하지 않는다.

아래 DB·HTTP 구현 관찰과 당시 미완료 항목은 본문에 명시한 #115/#116 커밋에 고정된 기록이다.
후속 수정의 현재 상태와 실제 배포 입력은 [Executor 운영 문서](../../services/cutover-executor/README.md)와
현재 T12 이슈에서 확인한다. 이 기록을 이미 해결된 결함의 재구현 요청으로 사용하지 않는다.

## PgBouncer

두 풀러 모두 전환 전에는 출발지 DB를 가리킨다. T12가 두 풀러를 PAUSE하고,
설계 8.6의 펜싱·barrier·commit decision 뒤 목적지로 바꾼다.

| 항목 | 계약 |
|---|---|
| 파드 | `replicas: 1`, Deployment `strategy: Recreate`, HPA 없음 |
| 설정 | ConfigMap의 `pgbouncer.ini`를 initContainer가 emptyDir의 `/etc/pgbouncer/pgbouncer.ini`로 복사 |
| 소유·모드 | 주 컨테이너와 동일 UID/GID, 파일 0600; 읽기 전용 root filesystem 유지 |
| 인증 파일 | `/etc/pgbouncer-auth/userlist.txt` Secret 볼륨은 읽기 전용 유지 |
| 관리 계정 | `cutover_control`, 관리 DB `pgbouncer`, 현재 TCP 5432 |
| 재접속 지연 | 개발 초기값 `server_login_retry = 2`; 장애 실측으로 조정 |
| TLS | 클라이언트 TLS, backend `verify-full`; 두 DB CA/인증서 이름을 전환 전에 확인 |

Recreate는 배포 교체 시 겹침을 줄이지만 단일 파드의 영구 보장이 아니다.
Executor는 PAUSE 직전 및 재접속 시 selector에 해당하는 파드와 UID를 읽고,
Ready 파드가 정확히 하나이고 종료 중인 이전 파드가 없음을 확인한다.
다른 endpoint·수동 파드·HPA가 없어야 한다. 연결 도중 UID가 바뀌면 재판정한다.

설정 변경은 같은 emptyDir에서 임시 파일 작성 → 원자적 rename → RELOAD →
SHOW DATABASES 및 실제 backend 접속 대조 순서다. RELOAD 반환만으로 완료 처리하지 않는다.
지속할 목적지는 ConfigMap에도 관측한 resourceVersion에 대한 충돌 검사와 함께 반영하고
다시 읽는다. 동시 배포가 목적지를 출발지로 되돌리지 않게 한다. 파드 재생성은 ConfigMap에서
다시 시작하므로 파일/ConfigMap 사이에 장애가 나면 옛 DB로 다시 연결될 수 있다.
쓰기 차단·펜싱·DB 쌍 lease를 유지하고 T12 실행 기록/실제 상태로 복구한다.
ConfigMap 불일치·결과 불명은 쓰기 재개 근거가 아니다.

기본 15초의 server_login_retry는 서버 로그인 실패 후 새 연결을 거부하는 대기다.
2초는 이 지연을 줄이는 선택이며 2초 내 컷오버/복구를 보장하지 않는다.
계획된 전환에서는 먼저 PAUSE하며 로그인 실패 주입·복구와 실제 앱 경로를 별도로 시험한다.
[PgBouncer 설정](https://www.pgbouncer.org/config.html#server_login_retry),
[관리 콘솔](https://www.pgbouncer.org/usage.html).

## Kubernetes 권한

`infra/envs/cutover-access/render.py`는 기존 `bravo-executor`에 추가할 Role/Binding만
렌더링한다. T7의 기본 Role/SA는 변경하지 않는다. 앱·풀러 namespace마다 적용하며
동일 클러스터 내 다른 namespace의 SA를 사용할 때 `--identity-namespace`를 명시한다.

| 자원 | 허용 | 범위 |
|---|---|---|
| pods | get, list | 해당 workload namespace |
| pods/exec | get, create | 검토한 현재 앱 파드들과 풀러 파드의 정확한 이름 |
| configmaps | get, patch | 해당 풀러 ConfigMap 이름 하나 |
| Secret API, Pod 생성, RBAC 수정, SA 토큰 발급 | 불허 | 기존 부여 권한도 함께 검사 |
| DB/운영자 파드 exec | 불허 | 실제 거부 시험 |

GET/POST exec 클라이언트를 지원한다. 출발지 namespace에는 DB도 함께 있어 전체 exec는
DB 제어 역할 분리를 우회할 수 있다. RBAC는 명령·컨테이너·label별 exec 제한을 제공하지 않는다.
허용 파드의 모든 컨테이너와 내부 자격증명은 사실상 접근 가능하다.
“Secret API를 읽지 않는다”와 “비밀에 접근할 수 없다”는 다른 주장이다.
교체 파드의 새 이름은 자동 허용되지 않는다. 배포 소유자가 owner/UID/image 및 전체 앱 목록을
확인하고 Role을 다시 적용·검증한 뒤 실행을 재개한다. Executor 자체 승격은 허용하지 않는다.

앱 토큰은 파드 안 ADMIN_TOKEN을 이용해 localhost 관리 API에 넘긴다. 토큰·헤더는 출력하지 않는다.
`verify.py`는 인증된 GET `/admin/write-block`, pooler 설정 디렉터리 임시 파일 생성/rename/정리,
실제 거부 시험을 수행한다. 차단 상태 변경·RELOAD는 하지 않는다. ConfigMap patch는 권한 판정만
검사하며 목적지 변경/영속화는 별도 컷오버 시험에서 다룬다. SA 토큰은 메모리/stdin에만 유지한다.

## 네트워크와 DB 역할

플랫폼 Executor 통로는 복제용 통로와 분리한다. 새 정책 전에 T8/T30 소유자의 현재
태그/주소/포트를 대조한다. 개인 장치나 Collector를 출발 주체로 쓰지 않는다.

| 시작 | 도착 | 포트·인증 | 완료 증거 |
|---|---|---|---|
| 플랫폼 Executor | source/target K8s API | 각 private API 포트·CA, 별도 단기 SA 토큰 | 실제 권한·거부 시험 |
| 플랫폼 Executor | source PgBouncer | TCP 5432, TLS, cutover_control / pgbouncer | 플랫폼 실행 위치에서 SHOW DATABASES |
| 플랫폼 Executor | target PgBouncer | TCP 5432, TLS, cutover_control / pgbouncer | 플랫폼 실행 위치에서 SHOW DATABASES |
| 플랫폼 Executor | source DB primary | TCP 5432, verify-full, T12 DB 제어 역할 | 필요한 제어만 허용·앱/Collector 거부 |
| 플랫폼 Executor | target DB primary | TCP 5432, verify-full, T12 DB 제어 역할 | 같은 시험, 반대편 쓰기 차단 유지 |

기존 T8 복제 경로는 PgBouncer를 노출하지 않는다. 별도 태그 후보는
`bravo-platform-executor`, `bravo-source-pooler-admin`, `bravo-target-pooler-admin`이며 아직 생성·승인된
네트워크 정책이라는 뜻은 아니다. T8의 `bravo-source-db`, `bravo-target-db`를 재사용하되
Executor에서 TCP 5432만 허용하는 별도 규칙을 검토한다. API는 기존 private 경로를 우선한다.
ClusterIP와 `.svc.cluster.local`은 다른 클러스터에서 저절로 해석·접속되지 않는다.
주소·CA·Secret 참조는 비공개 접속 계약에 기록하고 인증값은 문서/PR에 넣지 않는다.

기존 `bravo-target-pooler`는 target→source DB 접속용 egress 주체이므로
풀러 관리 콘솔 ingress 태그로 재사용하지 않는다.

현재 출발지 cutover_control은 pooler 계정이며 PostgreSQL 역할이 아니다.
T12 담당자가 [11/3 20:12에 공유한 DB·복제 요구](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793704349319319)는 다음과 같다.
구현은 [#115](https://github.com/Softbank-Hackathon-Finals/platform/pull/115)
`d90988ccbb6f8add652f054f3710362cf1c96494`와 그 위에 쌓인
[#116](https://github.com/Softbank-Hackathon-Finals/platform/pull/116)
`cccddc0ab1a9aef84de8e73dc463603cc8ea9581`에서 확인했다.
아래는 `services/cutover-executor/testing/sql/10-roles.sql`, `30-cutover.sql` 및
`src/bravo_cutover/remote/db.py`와 대조한 계약이다. 모의 환경 SQL을 기존 DB에
그대로 실행하지 않는다. 역할 생성, publication 이름, 구독 소유자는 환경별 이관 대상이다.

| 목적 | T12가 요청한 권한/조건 | 검증 경계 |
|---|---|---|
| 펜싱 | CREATEROLE + chat_app ADMIN OPTION, 앱 권한 상속 없음 | 직접/SET ROLE 앱 쓰기 권한을 얻지 않는지 확인; 단순 NOINHERIT만으로 SET ROLE을 막았다고 주장하지 않음 |
| 세션·긴 트랜잭션 | pg_signal_backend, pg_read_all_stats | 실제 필요한 세션 종료·관측과 superuser 세션 거부 확인 |
| 구독 켜기/끄기 | 구독 소유 역할로 SET ROLE, 평상시 권한 상속 없음 | 간접 SET ROLE까지 역할 그래프와 실제 효과 확인; postgres 역할 부여 금지 |
| 시퀀스 | UPDATE, setval은 올리기만 | UPDATE 자체가 감소를 막지는 않음; Executor 단조 증가 검증 필요 |
| barrier·비교 | cutover.barrier INSERT/SELECT, cutover.probe SELECT, 비교 테이블 SELECT | 기본 제어 역할의 업무 테이블 DML 거부와 구독 소유 역할 전환 뒤 권한을 구분 |
| 역복제 | 미리 생성하되 disabled, copy_data=false, origin=none | 실행 중 생성하지 않고 ENABLE/DISABLE; 양방향 동시 쓰기 불허 |
| 구독 소유자 | LOGIN 필요, 비밀번호는 없어도 됨 | 담당자의 k3d 관측이며 실제 PG18.4에서 확인 |
| publication | barrier/probe용 cutover 스키마 포함, chat_app은 probe INSERT | 기존 publication에 두 테이블 추가 후 구독 REFRESH/readback; 업무 테이블 재복사 방지 |
| T13 | compare(src,dst,tables) → verdict/hash_rule/tables | 임시 행 수 비교 UNVERIFIED는 운영 통과 금지 |

CNPG의 inline managed.roles는 DB catalog를 주기적으로 선언에 맞춘다.
현재 `chat_app login:true`는 SQL NOLOGIN 펜싱과 충돌한다. 역할 제어의 소유자를
분리한 뒤 장시간 NOLOGIN 유지와 반대편 LOGIN 전환을 시험해야 한다.
이관 시 기존 역할을 inline `managed.roles` 목록에서 제외하고 후속 render가 다시
등록하지 않게 한다. `ensure: absent`는 역할 삭제 요청이므로 사용하지 않는다.
Secret 비밀번호 회전도 새 역할 소유자의 절차로 명시한다.
속성만 지우면 기본값이 적용될 수 있으므로 해결됐다고 가정하지 않는다.
[CNPG 1.30 역할 관리](https://cloudnative-pg.io/docs/1.30/declarative_role_management/).
이 PR은 T8 소유의 live DB 역할을 임의 변경하지 않으며, 전체 컷오버 준비 판정을 하지 않는다.

T8 renderer의 발행 이름은 `source_chat` / `target_chat`, 구독은 출발지의
`t8_target_to_source`와 목적지의 `t8_source_to_target`이다. 실제 catalog와 대조한 뒤
T12 `Site.subscription`에 넣는다. source/target 모두 `chat_replicator`를 사용하므로
모의 환경의 `replicator`를 그대로 쓰지 않는다. 구독 `run_as_owner=false`에서는 소유자가
각 복제 테이블 소유 역할로 SET ROLE 할 수 있어야 한다. Executor → 구독 소유자 →
테이블 소유자로 이어지는 권한도 포함해 검토한다. `chat_app SET FALSE`만으로 Executor의
모든 업무 쓰기가 금지됐다고 판단하지 않는다. 테이블이 postgres 소유라면 소유권 이관 또는
별도 제한된 제어 경로를 먼저 결정한다. 단순히 postgres 역할을 부여해 통과시키지 않는다.
[PostgreSQL 18 복제 권한](https://www.postgresql.org/docs/18/logical-replication-security.html),
[구독 갱신·소유권](https://www.postgresql.org/docs/18/sql-altersubscription.html).

앱에는 [WebSocket 종료 요청](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793703703704249)도 있다.
제안은 POST `/admin/websockets/close`, 기존 ADMIN_TOKEN 인증, 프로세스별 1012 종료와 closed 수 반환이다.
Executor가 모든 앱 파드에 호출하므로 이미지의 Python과 localhost:8080 관리 경로를 유지해야 한다.
배포 전 demo-chat의 실제 구현/이미지 digest로 확인한다.

## T10 소비 계약

위 #116 head의 `api.py`, `contract.py`, `store.py` 기준 계약은 아래와 같다.
새 head에서는 변경된 요청·응답과 실패 처리를 다시 대조한다.

- `POST /v1/cutovers`: 새 실행 202, 동일 키·동일 입력 재요청 200, Idempotency-Key 필수.
  둘 다 실행 객체를 반환한다. 202는 접수이며 완료가 아니다.
- `GET /v1/cutovers/{id}`: 실행 조회.
- `GET /v1/cutovers?plan_id=&epoch=`: 응답 유실 시 조회. 조회 실패/결과 불명은
  새 epoch/키로 다시 시작할 근거가 아니다.
- 단계 status는 started/succeeded/failed/unknown을 execution.updated에 그대로 사용한다.
  실행 전체 결과 DONE/ABORT_PRE_WRITE/SAFE_HOLD와 혼동하지 않는다.
- DB 쌍 epoch/lease는 Executor가 소유한다. T10이 TTL/마지막 이벤트만 보고 SAFE_HOLD
  lease를 해제하거나 쓰기를 열지 않는다.
- SSE에는 기존 schema의 execution_id, step, status, 선택 elapsed_ms만 넣는다.
  producer key, 최초 occurred_at, run_id는 재전송에서도 유지한다.
- 요청 전 현재 승인·게이트·계획/diff/image 결속·만료를 검증한다. 이벤트 기록은 실행 허가가 아니다.
  [T10 이벤트 계약](T10-events-v1-proposal.md).

| 요청/응답 항목 | 게시 구현 |
|---|---|
| POST body | `plan_id`, `epoch`(양수), `source`/`target`의 `environment_id`와 `connection_ref`, `write_pause_budget_ms`(1~600000), 시간대가 있는 `deadline`; 그 밖의 필드 거부 |
| 실행 조회 | `execution_id`, `plan_id`, `epoch`, `status`, `steps`, `result`, `committed`, `created_at`, `updated_at` |
| 전체 상태 | `PENDING`, `RUNNING`, `DONE`, `ABORT_PRE_WRITE`, `SAFE_HOLD` |
| 400 | `idempotency_key_required` |
| 409 | `idempotency_key_conflict`, `stale_epoch`, `lease_held` |
| 422 | `deadline_passed` 또는 요청 schema 검증 실패 |
| 404 | `execution_not_found` |

현재 구현에서 `connection_ref`는 저장·멱등 입력에 포함되지만 원격 대상 선택은
`environment_id` → 서버 `Site` 설정으로 이뤄진다. T10의 승인된 접속 참조와 Site가
일치하는지 결속 검증을 추가해야 한다. 단계 배열에는 개별 발생 시각/식별자가 없으므로
poll마다 현재 시각을 붙여 같은 producer key로 재전송하지 않는다. 최초 관측 기록을
영속화하거나 Executor의 안정된 단계 이벤트 식별자를 사용해 T10 재전송 계약을 만족시킨다.

## 실행 프로세스와 배포 전 확인

`EXECUTOR_DB`는 Executor 소유 `executor.executions` / `executor.fences`와 DB 잠금에
사용한다. 기존 플랫폼 DB 안의 전용 스키마와 분리된 로그인을 우선 대조한다. T10 runtime
자격증명은 재사용하지 않으며 별도 물리 DB를 전제로 하지 않는다. 현재 `create_app`은
시작 시 `store.migrate()`로 CREATE/ALTER를 실행한다. migration 소유자와 상시 실행 계정의
권한을 나누려면 실행 경로도 분리해야 한다. 현재 구현에 없는 분리를 배포 완료로 표시하지 않는다.

`EXECUTOR_CONFIG`가 없으면 `NoRunner`가 기록만 접수하므로 `/healthz`의 ok로 원격 실행
준비를 판정하지 않는다. 설정에는 환경별 K8s context, 실제 selector/ConfigMap/구독 이름과
풀러 관리·풀러 앱·DB 제어·DB 앱의 네 DSN이 필요하다. DSN과 토큰은 Secret 참조/보호된
런타임 설정으로 전달하고 PR·로그에 넣지 않는다. source의 풀러 selector는
`app=source-pgbouncer`, ConfigMap은 `source-pgbouncer-config`이며 모의 환경 기본값과 다르다.

게시 API에는 인증 미들웨어가 없다. T10의 승인 검증과 전용 내부 호출 주체를 배포 경계에서
강제한 뒤 연결한다. 단순 네트워크 도달이나 기록 API 성공으로 승인된 컷오버를 대신하지 않는다.
현재 #116에는 배포 이미지 정의가 없으므로 패키징·이미지 digest·실제 Deployment도 확정해야 한다.

위 head에서 확인한 배포 전 수정 항목은
[소유권 상실 뒤 변경 지속 및 ConfigMap 실패의 DONE 처리](https://github.com/Softbank-Hackathon-Finals/platform/pull/116#pullrequestreview-5751688678)다.
수정 후 단계 내부 소유권 상실, ConfigMap 충돌/실패, 파드 UID 교체를 실패 주입으로 다시 확인한다.
필수 CI 성공과 k3d 성공만으로 이 조건이나 실제 환경 시험을 충족했다고 표시하지 않는다.

통합 완료에는 양쪽 네트워크/권한 실측, 실제 T12 서버의 동일 키 중복·응답 유실 조회,
하위 epoch 거부, SAFE_HOLD lease 유지, 정상 컷오버 및 ABORT_PRE_WRITE/SAFE_HOLD 각 1회가 필요하다.
k3d의 0.001초 설정 교체 수치는 클라우드 전체 쓰기 정지 시간으로 사용하지 않는다.
