# T12와 플랫폼·워크로드 연결 계약

상태: 구현 및 검증 제안. 정본은 `docs/design.html` 7.2·7.5·8.6·8.10이다.
실행 소유자는 T12, 배포 소유자는 T6/T30, 플랫폼 소비자는 T10이다.
이 문서는 실행 순서·승인·실패 분기를 바꾸지 않으며 각 이슈의 전체 완료를 뜻하지 않는다.

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
`bravo-platform-executor`, `bravo-source-pooler`, `bravo-target-pooler`이며 아직 생성·승인된
네트워크 정책이라는 뜻은 아니다. T8의 `bravo-source-db`, `bravo-target-db`를 재사용하되
Executor에서 TCP 5432만 허용하는 별도 규칙을 검토한다. API는 기존 private 경로를 우선한다.
ClusterIP와 `.svc.cluster.local`은 다른 클러스터에서 저절로 해석·접속되지 않는다.
주소·CA·Secret 참조는 비공개 접속 계약에 기록하고 인증값은 문서/PR에 넣지 않는다.

현재 cutover_control은 pooler 계정이며 PostgreSQL 역할이 아니다. DB 제어 SQL 목록
(앱 NOLOGIN/LOGIN, 세션 종료, barrier, sequence, subscription 제어)과 최소권한 역할은
T12 코드/ADR을 대조해 별도로 확정한다. 근거 없이 superuser/CREATEROLE이나 DB exec를
주지 않는다. CNPG managed role의 `login: true` 재조정과 펜싱의 충돌도 실제 구현에서 확인한다.

## T10 소비 계약

알려진 API는 아래와 같다. 서버 OpenAPI, 요청/응답 schema, 인증 및 오류 코드는
T12가 게시한 구현과 대조한 뒤 확정한다. 경로만으로 HTTP 어댑터를 연결하지 않는다.

- `POST /v1/cutovers`: 202, Idempotency-Key 필수. 202는 접수이며 완료가 아니다.
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

통합 완료에는 양쪽 네트워크/권한 실측, 실제 T12 서버의 동일 키 중복·응답 유실 조회,
하위 epoch 거부, SAFE_HOLD lease 유지, 정상 컷오버 및 ABORT_PRE_WRITE/SAFE_HOLD 각 1회가 필요하다.
k3d의 0.001초 설정 교체 수치는 클라우드 전체 쓰기 정지 시간으로 사용하지 않는다.
