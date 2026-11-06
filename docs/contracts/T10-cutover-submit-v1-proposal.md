# T10 CUTOVER 접수와 실행 관측

현재 구현은 **신뢰된 호스트에 붙이는 승인·접수 어댑터**다. T19 정본 조회, T17 SHIFT, 사용자 로그인, 전체 계획 상태 머신의 구현이나 실제 컷오버 완료를 뜻하지 않는다. 설계 FR-7, 8.2, 8.6, 8.9와 [T5 소비 계약](../controls/README.md)을 따른다.

## 승인과 실행 순서

1. 호스트가 검증한 `Principal`로 현재 tenant의 계획을 찾는다. 클라이언트 tenant 헤더는 인증으로 사용하지 않는다.
2. 계획이 `SHIFT`이고, `CutoverAuthority`가 동일한 계획 쓰기 잠금 안에서 현재 실행 조건을 검증해야 승인할 수 있다. 이 어댑터에는 계획을 SHIFT로 건너뛰는 API가 없다.
3. `POST /v1/plans/{id}/approvals`는 `approval_kind=cutover`, 현재 `binding`, `expected_version`, `Idempotency-Key`를 받는다. 승인 ID, 승인자, 시각, 계획 버전, 서버의 불변 명령과 authority revision을 이벤트와 함께 저장한다. S2 `change` 승인은 거부하며 T15/T31에 대한 승인을 발급하지 않는다.
4. `POST /v1/plans/{id}/cutover`는 승인 ID와 새 `Idempotency-Key`만 받는다. 클라이언트는 epoch, 실제 DB 접속 참조, deadline이나 실행 예산을 정하지 않는다. 현재 승인자의 권한·폐기 여부와 실행 요청자의 권한을 다시 검사하고 현재 계획·diff·이미지·게이트·만료·설정 revision을 저장된 승인과 대조한다.
5. 명령 ID, 승인 소비, 계획 내 epoch 사용, `SHIFT → CUTOVER` 및 이벤트를 기존 DB 트랜잭션에 저장한다. 커밋에 성공한 최초 호출자만 이 명령 ID를 Executor 멱등 키로 사용해 POST 한 번을 보낸다.
6. 같은 요청 재시도나 프로세스 재시작은 저장된 명령을 찾아 GET으로만 관측한다. POST 응답을 잃거나, 커밋 뒤 전송 전에 프로세스가 죽으면 자동 재전송하지 않는다. 빈 조회 결과도 UNKNOWN이다.
7. 관측된 실행 ID·계획·epoch·이력의 일관성을 확인하고 단계별 `execution.updated`를 같은 DB 트랜잭션에 저장한다. 키는 실행 ID와 단계 ordinal에 결속되며, 시각은 최초 관측 시각이다. 반복 조회는 이벤트를 다시 만들지 않는다.

`POST /v1/plans/{id}/cutover/{command_id}/reconcile`는 같은 조회·저장 경로를 명시적으로 실행한다. 새 원격 명령을 보내지 않는다. 기존 계획 GET과 SSE에 같은 principal 정책을 사용한다. 반환 관측값은 allowlist를 따르며 원격 detail/result, 접속 참조와 자격은 반환하지 않는다.

## 호스트가 제공해야 하는 검증

`CutoverAuthority(connection, principal, plan, now, approved_by, approval_id)`는 검증 후 `CurrentCutover(binding, command, revision)`을 반환한다. **이 객체 생성 자체는 검증이 아니다.** 기본 구현은 항상 거부하고 HTTP 503을 반환한다. 지금 저장소에는 이 포트에 연결할 운영 T19/SHIFT 정본 저장소가 없다.

호스트는 다음을 읽고 검증해야 한다.

- 인증된 T19 서비스의 불변 정본·현재 평가 context·전체 판정·필수 행·현재 예외의 유효성. 입력 hash나 이벤트의 PASS만으로 통과시키지 않는다.
- 현재 계획, diff, 배포 이미지 digest, 목적지, 게이트 ID와 context digest, 승인 만료와 폐기, 원래 승인자 및 현재 요청자의 권한.
- 실제 SHIFT 100% 및 지연 기준 충족, 데이터 준비, 보안 보류와 실행권, 등록 DB 자원·접속 참조·설정 revision.

이 조건을 바꾸는 모든 writer는 같은 계획 잠금을 사용해야 한다. 원격 조회를 잠금 전에 캐시한 결과나 boolean만 돌려주는 대체 구현은 통합 검증을 충족하지 않는다. 승인 소비와 원격 실행 사이의 결정 시점은 **명령 접수 커밋**이다. 이미 접수한 원격 명령은 승인 폐기로 취소됐다고 표시할 수 없다.

## 저장·복구와 전송

- 기존 `plans`, `plan_commands`, `events`와 SQLite 쓰기 트랜잭션 / PostgreSQL 계획 advisory lock을 재사용한다. 스키마 변경이나 runtime 권한 확대는 없다. journal key는 전용 `cutover-` 접두사와 해시를 쓴다. 실행 관측은 불변 행으로 추가하며 기존 명령 영수증을 덮어쓰지 않는다.
- 조회 불명 또는 원격 SAFE_HOLD는 계획을 SAFE_HOLD로 저장한다. 이후 원격 DONE을 읽어도 보류를 자동 해제하거나 계획을 WATCH/DONE으로 올리지 않는다. 무결성 영수증·WATCH·완료 판정은 후속이다.
- 실행별 단계 이력의 변경·퇴행은 UNKNOWN이다. 원격 생성/갱신 시각을 개별 단계 발생 시각으로 바꾸지 않는다.
- Executor는 서버 설정의 한 DB 쌍과 connection_ref만 받는다. 이 어댑터의 계획별 epoch 기록은 T12의 실제 자원별 lease·단조 epoch 검사를 대체하지 않는다.
- 전송은 검증된 HTTPS와 메모리의 Bearer를 사용한다. 전용 CA는 `ssl.create_default_context(cafile=...)`로 만든 `SSLContext`로 주입한다. 인증서 검사와 hostname 검사를 모두 요구한다. 클러스터의 평문 HTTP는 허용하지 않는다. literal loopback HTTP만 시험용으로 유지한다.
- timeout, 오류 상태, 잘못된 응답, 초과 크기, 중복 JSON key는 UNKNOWN이다. POST 재시도·redirect·환경 프록시는 없다. 승인/명령 저장 실패 시 POST를 보내지 않는다.

## 실행 상태를 읽는 계약

접수와 재개는 같은 `ExecutorReader`를 소비한다. 이전 조회 전용 문서의 이 절을 여기로 통합했다. 조회 자체는 POST·자동 재시도·lease 해제·상태 전이를 하지 않으며, 위 접수 어댑터가 저장과 상태 전이를 맡는다.

| 관측 | 반환 | 호출자의 다음 행동 |
|---|---|---|
| 실행 ID·계획·epoch 일치, 정상 형식 | `OBSERVED` + 허용 필드 snapshot | 원격 보고로 보존하고 별도 승인/완료 조건과 대조 |
| 계획·epoch 조회에서 정확히 1개 | 해당 실행의 식별자까지 검증 | 신뢰된 명령 기록과 대조한 뒤 실행 ID를 영속 결속 |
| 빈 목록 / 404 | `UNKNOWN` | 원격 미실행으로 단정하거나 새 명령을 만들지 않음 |
| 복수 실행 / ID·계획·epoch 불일치 | `UNKNOWN` | 소유·결속 문제를 확인 |
| timeout / 5xx / 인증 거부 / redirect | `UNKNOWN` | 기존 hold와 명령을 유지하고 운영 경로 확인 |
| 이전 관측보다 짧거나 바뀐 단계 이력, 시간·commit 회귀 | `UNKNOWN` | 과거 snapshot을 덮어쓰지 않음 |
| 원격 `SAFE_HOLD` | `OBSERVED`, `remote_status=SAFE_HOLD` | 사람이 해제하는 계약 유지 |
| 원격 `DONE` | `OBSERVED`, `remote_status=DONE` | T10 `DONE`으로 자동 전이하지 않음 |

`UNKNOWN`은 T12가 제공하는 실행 상태와 구별되는 **조회 결과**다. `OBSERVED`도 안전·승인·무결성 판정이 아니다.

## 호출 경계

```python
from bravo_platform_events.executor import ExecutorReader, ExpectedExecution

# 아래 값은 서버의 명령/환경 기록에서 가져온다. 외부 HTTP 입력을 그대로 쓰지 않는다.
expected = ExpectedExecution("plan-demo", 7, "aaaaaaaa-aaaa-4aaa-8aaa-abcdefabcdef")
with ExecutorReader("https://executor.example.invalid") as reader:
    observation = reader.read(expected)
```

- `ExpectedExecution` 생성 자체는 인증이나 승인 증명이 아니다. 호스트가 principal/tenant와 계획 접근을 먼저 검증해야 한다. 읽기와 CUTOVER 접수는 같은 호스트의 접근 검증을 통과해야 한다.
- 원격 접속 origin은 서버 설정으로 고정하고 HTTPS 인증서 검증을 사용한다. URL의 사용자 정보·query·fragment·path를 거부하고 환경 proxy를 상속하지 않는다. HTTP는 시험용 literal loopback만 허용한다. `transport` 주입은 신뢰된 테스트/호스트 구성의 경계이며 별도 transport가 TLS를 끄지 않도록 호스트가 책임진다.
- `ExecutorReader(token=...)`는 조회 자격만 받는다. 승인 실행 호스트의 `ExecutorClient(token=..., command_token=...)`는 등록 및 실행 조회 GET에 조회 자격을, 명령 POST에 별도 실행 자격을 보낸다. 실행 자격이 없으면 명령을 전송하지 않고 UNKNOWN으로 반환한다. Bearer 전달 기능만으로 실제 서비스 인증·네트워크 경계의 구성을 증명하지 않는다.
- 응답은 256 KiB, 단계 목록은 256개, 단일 socket 작업은 기본 5초로 제한한다. 스트림 처리 중에도 경과 시간을 확인한다. redirect, 압축 응답, 중복 JSON 키를 거부한다. socket timeout은 전체 분산 작업의 취소나 deadline 보장이 아니다.
- snapshot에는 실행 ID, 계획, epoch, 원격 상태, commit 여부, 단계 이름·상태·시간, 실행 전체의 생성·갱신 시각만 담는다. 원시 `result`, 단계 `detail`, 예외 원문은 반환하지 않는다.
- `previous`를 주면 동일 실행의 append-only 단계 이력과 시간·commit 진행을 확인한다. `SAFE_HOLD` 또는 종료 상태를 임의로 다시 열지 않는다. 의도한 수동 해제는 별도 인수 절차로 처리한다.

실행 ID가 없는 복구 조회에서는 정확히 하나인 결과를 서버의 불변 명령 기록과 대조한다. T12 GET은 원래 요청 digest·환경 참조를 돌려주지 않으므로, 응답만으로 승인된 실제 DB 쌍을 증명할 수 없다. 위 접수 경로가 저장한 명령·승인·구성 revision과의 결속을 유지해야 한다.

조회 시험과 당시 AWS 관측 범위는 [읽기 어댑터 증빙](../evidence/t10-executor-read-2026-11-03/README.md)에 남긴다. 그 기록은 당시 커밋의 시험이며 이후 접수·인증·전체 통합의 증빙으로 확장하지 않는다.

## 검증과 남은 연결

합성 authority와 시험 전용 SHIFT seed는 tests에만 있다. SQLite와 실제 임시 PostgreSQL에서 승인 만료·폐기·현재 결속 변경, 타 tenant, 중복/동시 요청, 저장 실패, 커밋 후/응답 후 프로세스 유실, 재개 시 GET, 이벤트 중복 방지와 SAFE_HOLD 보존을 확인한다. 실제 loopback TCP 응답 유실과 전용 CA의 정상/미신뢰/잘못된 hostname도 시험한다.

운영 인증, T19/SHIFT authority, 실제 TLS Executor, 원격 DB·앱·T13 영수증을 연결한 통합 시험은 남아 있다. 현재 계획 모델을 SQL로 SHIFT에 바꾸거나 합성 gate/승인을 운영에 넣어 그 조건을 통과시키지 않는다. 운영 연결의 인수는 T10의 목업 기반 완료 기준과 별도로 확인한다.

### T19 원문의 계획별 저장·조회

`GateRecords`는 기존 계획 트랜잭션과 `plan_commands`에 양쪽 통제 원문,
현재 컨텍스트, 보안·합산 판정 원문을 저장한다. 같은 원문 참조의 변경,
다른 테넌트·계획·환경, 목적지 누락을 거부한다. 한쪽 결과만 먼저 저장하고
재조회할 수 있지만 양쪽이 갖춰지기 전에는 게이트 컨텍스트를 연결하지 않는다.

`T10GateAuthority.load_session`은 전달받은 연결로 `records.view()`를 만들고
그 view의 `session()`에서 `PlanGateSession`을 받아 사용한다. 호스트는 T19의
현재 생산자 인증·권한·T13 준비 공급자를 그대로 제공해야 한다. 저장 성공이나
검증 전용 계획은 운영 환경 등록·판정·승인을 대신하지 않는다.
[호스트 연결과 클러스터 검증 범위](../../services/platform-events/testing/GATE-RECORDS.md)

반영 뒤에는 성공한 Application 체크포인트에 묶인 `post_apply` 세션과
계획의 재대조 프로필 쌍을 사용한다. 최초 게이트 기록은 이를 대신할 수 없다.
[필드·단계·재시도 계약](T10-registration.md#반영-후-재대조-프로필)

### 계획 ID로 재시작하기 (T10 할 일 4)

호스트는 `WorkflowRecovery.resume_once(principal, plan_id)`로 한 단계씩 복구한다. 수집 종료는 기존 `complete_collection`, 최초 컷오버 접수는 기존 `CutoverService.submit`이 담당한다. 복구기는 Executor에 GET만 보낸다. [구현](../../services/platform-events/src/bravo_platform_events/workflow.py)

| 중단 당시 남은 기록 | 재시작 동작 |
|---|---|
| 분석부터 SHIFT 진입 전까지 저장한 계획과 증빙 | 생산자의 현재 증빙을 읽는다. 계획 버전, 목적지, 이전 단계의 결속값과 해당 단계 조건을 확인한 뒤 상태·증빙·이벤트를 한 트랜잭션에 저장한다. |
| 접수 기록은 있고 원격 실행은 조회되지 않음 | 기존 컷오버 조회 규칙에 따라 `SAFE_HOLD`에 멈춘다. |
| 원격 실행은 확정됐지만 응답 수신 전에 프로세스가 종료됨 | 저장된 승인 버전으로 명령을 찾고 계획·epoch로 조회한다. 같은 실행의 무결성 일치 영수증을 확인한 뒤 `WATCH`로 간다. |
| WATCH에서 재시작 | 결속된 실행을 다시 읽는다. 해당 무결성 영수증에 대한 감시·통제 재시험이 모두 PASS이면 `DONE`을 기록한다. |

증빙 조회는 신뢰된 호스트 전용이다. 호스트는 각 담당 서비스의 정본, 현재 보류·권한 회수, 증빙 유효 기간을 같은 계획 잠금 아래 확인해야 한다. 모의 시험은 생산자의 판정을 대역으로 제공한다. 운영 생산자 연결과 T18 실제 창 수신은 각각의 인수 절차에서 확인한다.

재현하려면 `services/platform-events`에서 `uv sync --locked` 후 `uv run pytest -q tests/test_workflow_recovery.py`를 실행한다. PostgreSQL 서버 도구가 필요하다. 시험은 독립된 HTTP Executor와 임시 DB를 사용하며, 적용 직전과 적용 직후에 플랫폼 프로세스를 강제 종료한다. [재시작 시험](../../services/platform-events/tests/test_workflow_recovery.py)

### 배포된 Executor의 DB 쌍 확인

호스트가 `create_operator_cutover_app(..., registered_pair=...)`로 시작하면 T10 클라이언트가 인증된 `GET /v1/resources`를 한 번 보낸다. 입력은 최초 등록 기록에서 읽은 두 환경의 `environment_id`, `resource_id`, `connection_ref`다. 최초 등록 기록의 `source`와 `target`이 방향을 정한다. 두 환경 ID, 두 DB 자원 ID, 두 연결 참조는 각각 서로 달라야 한다. 방향과 여섯 값이 모두 같을 때만 응답의 연결 참조로 서비스를 구성한다. 인증 실패·조회 실패·등록 변경이면 앱 구성을 중단한다. 이 조회는 시작 시점의 등록 확인이다. 실행 권한과 매 요청의 승인·게이트 검사는 기존 경로에서 판정한다. [T12 조회 계약](T12-platform-integration.md#등록된-출발지와-목적지-조회)
