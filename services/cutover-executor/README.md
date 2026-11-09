# bravo-cutover (T12)

Cutover Executor. 상태 머신(T10)의 호출을 받아 쓰기 정지 구간(DB 쓰기를 잠깐 멈추고 쓰기 대상을 출발지에서 목적지로 바꾸는 몇 초)을 8단계 순서대로 실행한다. 순서 · 실패 처리 · 호출 계약은 [설계 문서 8.6](../../docs/design.html), 언어 결정은 [ADR-0006](../../docs/adr/0006-cutover-executor-language.md).

- **쓰기를 받는 DB는 어느 순간에도 최대 하나다.** 상태를 확인할 수 없으면 아무것도 열지 않고 SAFE_HOLD로 멈춘다.
- **LLM을 쓰지 않는다.** 소스 import, 잠금 파일의 전체 의존성, 실제로 import된 모듈을 테스트로 검사한다(`tests/test_no_llm.py`).

## 개발

```bash
uv sync
uv run pytest
uvx ruff check .
```

로컬 API는 실행 기록 DB(`EXECUTOR_DB`), [환경 설정](testing/mockenv.py)(`EXECUTOR_CONFIG`), 조회 토큰 파일(`EXECUTOR_API_TOKEN_FILE`)을 준비한 뒤 기동한다. 명령 접수를 시험할 때만 별도 파일을 `EXECUTOR_COMMAND_TOKEN_FILE`로 지정한다.

```bash
uv run uvicorn bravo_cutover.server:create_app --factory --host 127.0.0.1 --port 8080
```

요청의 `environment_id` · `connection_ref`는 이 등록 정보와 맞아야 하고, lease · epoch는 `resource_id`마다 걸린다. 같은 DB를 다른 환경 이름으로 등록해도 같은 `resource_id`면 겹치는 실행으로 막힌다.

## 조회와 컷오버 명령 자격

- 기존 `api-token`은 조회용이다. 이 자격으로 GET을 허용하고 POST를 403으로 거부한다.
  컷오버 접수에는 별도로 생성한 명령 토큰이 필요하다. 두 값이 같으면 실행기를 시작하지 않는다.
  배포 전에 운영자는 `cutover-executor-command` Secret의 `token` 키를 준비한다.
  조회 서비스에는 조회 값만 공급한다. 명령 토큰은 승인 실행 호스트와 Executor에만 제공한다.
  이 버전의 서버를 기존 매니페스트로 기동해 `EXECUTOR_COMMAND_TOKEN_FILE`이 없으면 조회만 가능하다.
  새 매니페스트에 명시한 파일이 없으면 시작을 중단한다.
- 두 프로세스는 토큰을 기동 시 읽는다. 교체 전에 새 명령 접수를 중단하고 진행 중인 실행이 있는지 조회한다.
  Secret 교체 후 Executor와 승인 실행 호스트를 새 자격으로 재기동한 뒤, 조회 자격의 GET 성공과 POST 거부를 확인한다.
  구 이미지에서는 조회 자격으로도 명령을 보낼 수 있다. 그 이미지로 돌아가야 하면 먼저 읽기 전용 소비자의 접근을 차단하고 기존 공유 토큰을 폐기한다.

## DB 안 무결성 지문 (T13)

compare 단계의 비교기는 `db_fingerprint.py`의 `DbFingerprintComparer`(규칙 `db-sql-v1`) 하나다. 각 DB 안에서 지문을 계산하고 지문만 가져온다. 생산·소비 방향은 [ADR-0013](../../docs/adr/0013-integrity-fingerprint-producer.md)을 따른다.

- 진행은 전체 `verdict`가 `MATCH`일 때만이다. `MISMATCH` · `UNKNOWN` · 그 밖의 값은 영수증을 저장한 뒤 쓰기 전 실패(ABORT_PRE_WRITE)다.
- 시퀀스는 비교가 `MATCH`일 때만 `correct_sequence`로 올린다(올리기만). 다시 읽어 확인하지 못하면 영수증 `sequence_error`에 고정 코드를 남기고 쓰기 전 실패다.
- 행 수만 보는 임시 비교와 `allow_unverified_compare` 설정은 없다. 이 키가 남은 설정 파일은 불러오기에서 거부된다.

규칙 · 지원 타입 · 판정 · 연결 방법은 [계약 문서](../../docs/contracts/T13-db-fingerprint-v1.md)에, 시간과 전송량은 [로컬 측정 증거](../../docs/evidence/t13-db-fingerprint-2026-11-04/README.md)에 있다. `uv run python testing/measure_fingerprint.py --out results.json`으로 다시 잴 수 있다.

## 컷오버 전 데이터 준비 관찰 (T19 게이트 입력)

`POST /v1/readiness`(명령 자격)는 GATE의 데이터 준비 판정에 쓸 T13 관찰을 돌려준다. 판정은 하지 않는다(`readiness.py`).

- 요청: 등록된 출발지 · 목적지 환경, 표, 시퀀스 열, `max_age_seconds`. 설정(`settings.tables` · `sequences`)에 없는 표 · 열, 등록되지 않은 환경 · 방향은 DB에 닿기 전에 거부한다.
- 스키마 · 기본 키는 표마다 두 번 읽어 그 사이 변경을 잡는다. 복제 지연은 사전 확인과 같은 계산이고, 정방향 구독이 돌지 않으면 값을 비운다.
- 시퀀스는 계약대로 `correct_sequence`로 목적지를 올리기만 하고 다시 읽은 값(`readback_next`)을 낸다. 목적지 앱 역할이 로그인할 수 없고 붙은 세션이 없을 때만 올린다. 아니면 행을 빼고 `sequence_errors`에 코드를 남긴다.
- 끝나지 않은 실행이 있으면 409(`execution_in_progress`), 관찰이 유효 시간(최대 30초) 안에 끝나지 않으면 503(`readiness_capture_expired`)이다. 실패는 고정 코드만 내보낸다.
- 운영자는 `operations/gate/run.py capture`로 부른다. 승인 호스트(명령 자격 보유)가 이 경로를 부르고, GATE 릴레이가 현재 context에 묶어 기록한다.

## 모의 환경 (k3d)

T8(복제) · T30(목적지) 전에 Executor를 시험하는 곳. 클러스터 하나에 출발지 `src` · 목적지 `dst` 네임스페이스를 두고 각각 Postgres 16 · PgBouncer 1.26.1 · demo-chat 2개를 띄운다. 가짜 AWS(moto)는 `aws` 네임스페이스. 복제는 직접 만든 구독으로 흉내 낸다: `dst`의 `bravo_fwd`(src → dst, 켜짐), `src`의 `bravo_rev`(dst → src, 꺼짐).

```bash
python3 testing/mockenv.py up      # 약 2분. demo-chat은 ../../../demo-chat의 origin/main으로 이미지를 만든다
python3 testing/mockenv.py check   # 관리 콘솔 · 복제 · 목적지 앱 → 출발지 DB 쓰기 · 목적지 앱 역할 로그인 거부
python3 testing/mockenv.py down
```

- 비밀번호는 실행마다 새로 만들어 `testing/.state/env.json`(git 제외)에만 둔다.
- PgBouncer 설정은 initContainer가 ConfigMap을 emptyDir로 복사한 파일이다. Executor가 파드 안 파일을 바꾸고 RELOAD한다(spec D3).
- 역할과 권한은 `testing/sql/10-roles.sql` · `30-cutover.sql`. `cutover_control` 권한이 Executor에 필요한 권한 목록의 초안이다(실제 환경은 T6 · T8 담당과 확정).

## 플랫폼 배포와 실제 환경 계약

운영 시작점은 `bravo_cutover.server:create_app`이다. 이 factory는 등록된 **한 DB 쌍**,
검증된 비교기, 파일에서 읽는 32자 이상의 API 토큰을 요구하고 시작 시 DDL을 실행하지 않는다.
명령 자격을 인증했더라도 실행기나 등록 정보가 준비되지 않으면 POST는 503이다.

- `env-aws` / `cutover/source`, `env-onprem` / `cutover/target`를 `sites.json`에 등록한다.
  `resource_id`로 실제 DB를 식별한다. `environment_id`와 `connection_ref`가 모두 일치하는 두 방향만 받는다. 별칭, 한쪽만 공유하는
  제3의 DB 쌍, 참조 교환은 기록/실행 전에 422로 거부한다. 이 배포는 다중 DB 쌍 서버가 아니다.
- `sites.json` 최상위에 원래 출발지 `"origin": "env-aws"`를 둔다(T20). 없거나 등록된 환경이 아니면 시작을 거부한다.
  설정은 `extra="forbid"`라서 옛 이미지는 `origin`이 있는 설정을, 새 이미지는 없는 설정을 거부한다(실행 중이면 재시작 판정도 못 한다).
  이미지와 `cutover-executor-runtime` Secret의 `sites.json`은 같은 배포에서 함께 바꾸고, 끝나지 않은 실행이 없을 때만 교체한다.
- 모든 `/v1/` 요청에 Bearer 인증을 적용한다. T10의 승인/현재 계획/게이트 검증은 별도 선행이며,
  API 토큰을 가졌다는 사실을 사람의 컷오버 승인으로 간주하지 않는다.
- `python -m bravo_cutover.deploy --image <registry/repository@sha256:...>`는
  `bravo-platform`의 단일 Replica/Recreate Deployment, SA와 ClusterIP Service를 출력한다.
  Pod label은 `app.kubernetes.io/name=cutover-executor`, 서비스 포트는 HTTPS 8080이다.
  네임스페이스·TLS·네트워크·원격 Role은 환경 담당자가 별도로 검증한 뒤 제공한다.
- `cutover-executor-runtime` Secret에는 `sites.json`, token 없는 tailnet HTTPS `kubeconfig`,
  `api-token`, `tls.crt`, `tls.key`, DB TLS CA 파일을 직접 공급한다. 앱/DB 접속 자격은
  플랫폼에서만 사용하며 로컬 파일·로그·PR에 복사하지 않는다. Kube API는 각 환경의
  관측된 앱·PgBouncer Pod exec와 해당 ConfigMap get/patch 권한만 가진다.
- `cutover-executor-state`의 `dsn`은 플랫폼 DB의 별도 `cutover_executor_runtime` 계정이다.
  state DSN은 `sslmode=verify-full sslrootcert=/run/state-ca/ca.crt`를 사용한다.
  `cutover-executor-state-ca` Secret의 `ca.crt`는 runtime과 migration Job에 각각
  읽기 전용으로 마운트하며, 업무 DB 자격이 있는 runtime Secret과 분리한다.
  이 계정은 `executor` schema USAGE와 executions/resource_fences/leases 테이블 SELECT/INSERT/UPDATE만 갖는다.
  DDL은 `--migration`으로 출력하는 일회 Job이 별도 `cutover-executor-migration` Secret을
  사용해 실행한다. 완료 뒤 migration Secret을 회수하고 runtime Deployment에는 마운트하지 않는다.
- DB 제어는 실제 PG18에서 `control_functions=true`를 사용한다. `provision.control_sql()`은
  자격값을 포함하지 않으며 기존 postgres 구독/테이블 소유권을 유지한다. 운영자가 별도로 만든
  `cutover_control` 로그인에 고정된 앱 LOGIN/세션 종료, 구독 ENABLE/DISABLE,
  messages 시퀀스 올리기 함수만 부여한다. Executor에 `postgres` 멤버십·CREATEROLE·
  앱 역할 ADMIN/SET·업무 테이블 INSERT·임의 setval 권한을 주지 않는다.
- CNPG의 `managed.roles`가 `chat_app`의 LOGIN 값을 재설정하지 않도록 운영자가
  해당 항목의 관리 인계를 먼저 확인한다(역할 삭제 아님). 기존 비밀번호/Secret 소유권을
  유지하고 양쪽 pooler가 목표 DB의 SCRAM 자격과 CA/호스트 이름을 사용할지 별도로 확인한다.
- 두 publication에 `cutover.barrier`와 `cutover.probe`를 추가하고 구독 테이블을
  `copy_data=false`로 갱신한다. 업무 테이블을 다시 복사하지 않는다. 초기 상태는
  source LOGIN, target NOLOGIN, forward ON, reverse OFF다.
- 앱은 `POST /admin/websockets/close`를 제공해야 한다. Executor는 source 각 Pod 안에서
  그 Pod의 `ADMIN_TOKEN`으로 호출하며, `{closed: nonnegative integer}` ACK 뒤 쓰기 차단을 해제한다.
  Pod 변경/응답 누락은 성공으로 세지 않는다. 기존 관리 API가 없는 앱 이미지는 통합 시험 대상이 아니다.

## 되돌리기 (T20)

되돌리기(failback)는 같은 9단계를 출발지 · 목적지를 뒤바꾼 요청으로 실행한다. 사이트마다 자기 쪽 구독(목적지가 받는 구독)을
가지므로 사전 확인 · 원상 복구 · 재시작 판정이 방향과 상관없이 같다. lease와 epoch는 DB 쌍 단위라 되돌리기는 컷오버보다
큰 epoch로 요청한다. 영수증의 `direction`은 설정의 `origin`으로 정한다: 원래 출발지를 떠나면 `cutover`, 돌아오면
`failback`, 둘 다 아니면 원격 조작 없이 쓰기 전 실패다. 등록 조회(`GET /v1/resources`)도 같은 `origin`을 source로 공개한다
(환경 이름은 고정하지 않는다. `origin`을 모르는 기록 전용 서버는 503). 승인 · Edge 복귀 · 기록은 상태 머신(T10)이 지휘한다.

사전 확인 끝에 출발지 `cutover.barrier`에 시험 행을 넣고 목적지에 `path_probe_timeout_s`(기본 10초, 쓰기 정지 밖) 안에
보이는지 확인한다([ADR-0016](../../docs/adr/0016-failback-replication-path-check.md)). 컷오버 방향에도 똑같이 적용한다.

- 오지 않으면 진입점 · PAUSE · LOGIN · 구독을 바꾸지 않고 ABORT_PRE_WRITE, `reason_code=replication_path_unconfirmed`.
  제한은 시험 행을 쓰기 전에 정하고, 제한을 넘긴 뒤 본 도착은 인정하지 않는다.
- 시험 중 DB에 쓰거나 읽지 못하면 상태 불명이다: 바꾼 것 없이 SAFE_HOLD(사전 확인 규칙, lease 유지).
- 결과의 `path_probe`에 도착 여부 · 소요 · 목적지 구독의 마지막 수신 시각을 남긴다. 수신 시각은 진단용이며 판정하지 않는다.
- `remote_changes_started`는 복구 대상 효과(진입점 · PAUSE · LOGIN · 구독)를 시작했는지다. 시험 행은 여기에 들지 않는다.
- 쓰기를 막은 뒤 본 barrier가 오지 않으면 되돌아갈 쪽을 열지 않고 원상 복구한다(ABORT_PRE_WRITE). 확인하지 못하면 SAFE_HOLD.

모의 시험은 `tests/test_failback_mockenv.py`다. 경로 단절은 publication에서 `cutover.barrier`를 잠시 빼서 만들고,
빠진 동안 쓴 행은 시험 정리에서 지운 뒤 publication을 되돌린다. 실제 환경의 첫 되돌리기는 T25에서 한다.

## 영수증과 실패 처리

비교 규칙 · 지원 타입 · 판정은 [T13 계약 문서](../../docs/contracts/T13-db-fingerprint-v1.md)가 원문이다.

비교 영수증은 **목적지 LOGIN 이전** 실행 DB에 저장되고 실패/재시작 결과에도 유지된다.
MISMATCH면 전환을 취소하며 시퀀스는 감소시키지 않는다. 마지막 ACK 뒤 ConfigMap을 영속화한 후에도
pooler Pod identity·실제 목적지·RESUME 상태를 다시 읽고, 바뀌면 DONE 대신 SAFE_HOLD와 lease 유지로 끝난다.

사전 점검은 기존 NOLOGIN·PAUSE·앱 쓰기 차단을 인수하지 않는다. 이미 차단된 환경은 상태를 보존한 채
거부한다. 확인된 기준선(앱/풀러 Pod identity와 초기 상태)과 원격 변경 시작 단계를 실행 DB에 먼저 기록한다.
변경 시작 전 실패·프로세스 유실은 복구 명령을 보내지 않으며, 시작 후에도 그 단계의 효과만 복원한다.
기준선이 없는 이전 실행이나 교체된 파드, 실행 범위 밖에서 바뀐 상태는 SAFE_HOLD로 보류한다.
이 기록은 다른 운영자의 동시 원격 변경을 원자적으로 잠그지는 않으므로, 실행 중 수동 변경은 금지한다.

현재 자동 시험은 임시 PostgreSQL과 원격 대역이다. `EXECUTOR_TEST_POSTGRES_IMAGE=postgres:18.4`
로 실제 배포 버전의 SQL 경계를 시험할 수 있다. k3d 시험은 해당 환경이 있을 때만 실행한다.
T12는 k3d 모의 환경의 정상 컷오버·ABORT_PRE_WRITE·SAFE_HOLD 시험으로 닫는다. 이미지 게시와 플랫폼 배포는 T7(#140),
실제 T8/T30 환경의 첫 컷오버와 두 분기 확인은 T25에서 한다.

실제 DB 준비는 `prepare.cluster_patch()`가 관측한 Cluster의 resourceVersion 조건으로
`chat_app` 관리 항목만 제거하고 제한된 `cutover_control` managed role/전용 Secret 참조와
정확한 TLS HBA 항목을 추가하는 패치부터 검토한다. 기존 복제 역할과 SQL LOGIN 상태는 바꾸지 않는다.
`prepare.pooler_trust_patch()`는 풀러의 backend CA mount만 별도 `cutover-backend-ca`로 바꾼다.
새 Secret에는 source/target의 공개 CA 합본만 넣으며 CNPG 자체 CA/서버 인증서는 수정하지 않는다.
풀러가 교체되면 새 UID/이름을 읽어 named-pod 권한을 다시 인가하고 플랫폼 위치에서 검증한다.

초기 target 앱은 source 자격으로 source-backed pooler에 붙는다. 현재 host-only retarget 계약은
양쪽 DB의 `chat_app` 자격이 같아야 하므로, 실제 준비 단계에서 target NOLOGIN을 유지한 채
source 자격/SCRAM을 target DB와 app Secret에 맞춘다. 이 일회 인계와 이후 암호 회전은
DB 운영자와 T9 전달 담당이 같은 source Secret 버전에 결속해 수행한다. 암호를 로그/로컬 파일에
남기지 않는다. 이전 `managed.roles` manifest를 재적용해 LOGIN을 자동 복구하지 않도록
운영자가 이관 패치를 후속 렌더에도 적용해야 한다. 위 함수는 패치 작성만 하며 실제 적용/준비 완료 증거가 아니다.
