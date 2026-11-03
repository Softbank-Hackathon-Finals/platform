# bravo-cutover (T12)

Cutover Executor. 상태 머신(T10)의 호출을 받아 쓰기 정지 구간(DB 쓰기를 잠깐 멈추고 쓰기 대상을 출발지에서 목적지로 바꾸는 몇 초)을 8단계 순서대로 실행한다. 순서 · 실패 처리 · 호출 계약은 [설계 문서 8.6](../../docs/design.html), 언어 결정은 [ADR-0006](../../docs/adr/0006-cutover-executor-language.md).

- **쓰기를 받는 DB는 어느 순간에도 최대 하나다.** 상태를 확인할 수 없으면 아무것도 열지 않고 SAFE_HOLD로 멈춘다.
- **LLM을 쓰지 않는다.** 소스 import, 잠금 파일의 전체 의존성, 실제로 import된 모듈을 테스트로 검사한다(`tests/test_no_llm.py`).

## 개발

```bash
uv sync
uv run pytest
uvx ruff check .
uv run uvicorn bravo_cutover.api:create_app --factory --port 8080
```

실행 설정: `EXECUTOR_DB`(실행 기록 DB), `EXECUTOR_RESOURCES`(환경별 실제 DB 자원 등록 파일).

```json
{"env-aws": {"resource_id": "cnpg-aws/chat", "connection_ref": "secret/source"},
 "env-onprem": {"resource_id": "cnpg-onprem/chat", "connection_ref": "secret/target"}}
```

요청의 `environment_id` · `connection_ref`는 이 등록 정보와 맞아야 하고, lease · epoch는 `resource_id`마다 걸린다. 같은 DB를 다른 환경 이름으로 등록해도 같은 `resource_id`면 겹치는 실행으로 막힌다.

## 무결성 지문 (T13)

쓰기 정지 구간의 최종 비교는 각 DB 안에서 지문을 계산하고 지문만 가져온다(`db_fingerprint.py`). 규칙 · 지원 타입 · 판정 · 연결 방법은 [계약 문서](../../docs/contracts/T13-db-fingerprint-v1.md)에, 시간과 전송량은 [로컬 측정 증거](../../docs/evidence/t13-db-fingerprint-2026-11-04/README.md)에 있다. `uv run python testing/measure_fingerprint.py --out results.json`으로 다시 잴 수 있다.

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
`api:create_app`의 실행기 없는 인스턴스는 조회만 가능하며 POST는 503이다.

- `env-aws` / `cutover/source`, `env-onprem` / `cutover/target`를 `sites.json`에 등록한다.
  `resource_id`로 실제 DB를 식별한다. `environment_id`와 `connection_ref`가 모두 일치하는 두 방향만 받는다. 별칭, 한쪽만 공유하는
  제3의 DB 쌍, 참조 교환은 기록/실행 전에 422로 거부한다. 이 배포는 다중 DB 쌍 서버가 아니다.
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

## T13 비교 규칙 v1

기본 비교기는 실제 행 수, PK 정렬 해시, 전체 행 내용 해시와 테이블/컬럼/제약 스키마 해시를 비교한다.
콜레이션이 있는 PK는 `C` 순서이며, 타입과 NULL/텍스트/바이너리/숫자/시간/JSON은 별도 태그를 쓴다.
문자열 컬럼의 비교 의미는 정렬 순서와 별도로 해시에 담는다. collation의 제공자·로케일·규칙·
deterministic 여부·기록/실제 버전을 비교하며, default는 해당 DB의 로케일 설정으로 해석한다.
이름과 클러스터별 OID는 제외한다. 기록 버전과 실제 제공자 버전이 다르거나 미지원 제공자면 UNKNOWN이다.
정확한 숫자는 Decimal 전체 정밀도를 유지하고 UTC timestamp·정렬된 JSON 키·길이 접두어 SHA256을 사용한다.
양쪽 업무 쓰기 차단과 복제 barrier 확인 뒤 각 연결의 repeatable-read/read-only snapshot을 비교한다.
공간/확장 사용자 타입 등 미지원 값은 UNKNOWN으로 거부하며 전체 PostgreSQL 타입 호환을 주장하지 않는다.
현재 demo-chat의 기본 타입·PK·컬럼·제약을 지원한다. DB 객체 소유자/ACL은 이 해시에 포함하지 않는다.

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
실제 T8/T30 happy path·ABORT_PRE_WRITE·SAFE_HOLD와 T15/T19/T31 선행을 충족해야 이슈를 닫는다.

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
