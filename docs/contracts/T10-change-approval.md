# T10 S2 변경 승인과 반영 접수

설계 [S2](../design.html#s6-2)·[상태 머신](../design.html#s8-2)·[API](../design.html#s8-9)의
변경 승인을 기존 계획 저장소에 연결한다. T15가 검증한 변경에 사람이 승인하고,
반영 직전에 같은 변경인지 다시 확인하는 T10 경계다.
T15 검증·재시도, T19 판정, T31 배포를 이 모듈에서 실행하지 않는다.

## 최초 PROPOSE 접수와 같은 제안의 소비

[InitialProposalRecords·InitialProposalExecution](../../services/platform-events/src/bravo_platform_events/initial_proposal.py)는
최초 제안의 T10 내부 접점이다. 기존 `plan_commands`와 계획 잠금을 사용한다.
별도 HTTP 경로·실행기·상태 setter를 추가하지 않는다. [ADR-0028](../adr/0028-initial-proposal-records.md).

1. `admit(principal, plan_id, tenant_id, task_id, expected_version, expires_at)`는 현재
   `PROPOSE`와 직전의 정상 Analysis 체크포인트를 요구한다. 서명된 TaskCatalog와
   등록 소스 조회기를 다시 읽는다. 같은 계획·버전에 입력 한 건을 저장한다.
2. `ProductInputAccess(..., purpose="t38-initial-proposal")`의 현재 별도 grant를 요구한다.
   기존 `t38-product-proposal` grant는 이 권한을 주지 않는다. 입력의 만료 연장,
   다른 principal·task·source로 바꾸는 재접수는 거부한다.
3. `preparation_fields`는 기록을 바꾸거나 client를 호출하지 않는다. `execute`는
   요청을 먼저 commit하고 첫 호출만 `client.submit`을 호출한다. 이후 호출과 재시작은
   같은 `request_id`를 `client.read`로 조회한다. 응답 유실·불명·접수 직후 중단에도 재전송하지 않는다.
4. T38의 `client.validate(intent, input, envelope)`는 확인한 실행·산출물의 **로컬 검사**다.
   provider 조회는 `submit/read` 또는 운영자 인수에서 끝내야 한다. T10은 현재 권한·만료·소스를
   다시 확인하고 인증된 결과의 정규화 필드만 저장한다. 원격 원문·토큰·임의 metadata는 저장하지 않는다.
5. `evidence_reader(publication=...)`는 저장된 제안과 실제 T7 발행 기록을 읽는다.
   T7 조회기는 빌드·스캔·발행·registry readback, workflow/run/attempt/artifact 출처,
   같은 제안에서 나온 정확한 diff를 확인한다. T10은 request·plan·source·proposal digest를 대조한다.
   이 영수증이 있어야 기존 `WorkflowRecovery`가 `PROPOSE → REHEARSE`를 수행한다.
6. `stored(plan_id, plan_version, input_ref, input_digest)`는 같은 Proposal을 읽는다.
   client 호출은 없다. 즉시 다음 REHEARSE에서는 이 경로의 발행 영수증과 workflow 체크포인트가
   정확히 같아야 한다. T15는 자신의 실제 Admission·task·source·Change와 이 값을 결속한 뒤 소비한다.

입력과 전달 형식의 정본은 위 Python 모델이다. 입력은 `binding`, `run_id`, `manifest`, `files`다.
binding은 계획·tenant·버전·task·source·collection scope·inventory·comparison·Analysis 영수증을 고정한다.
`InitialIntent`에는 `request_id`, `plan_id`, **접수된 `plan_version`**, `input_ref`, `input_digest`,
`binding_digest`, `runtime_source_sha`, `recipient_public_key`, `purpose`가 있다.
읽는 쪽은 현재 최신 버전을 추측하지 않는다. `request_id`는 키 준비 전 필드와 principal을 결속한다.
한 번 접수한 `recipient_public_key`도 바꿀 수 없다.

`InitialResult`는 요청·입력·런타임 소스·실행 영수증·Proposal/digest·실제 schema/lint 결과를 담는다.
T10은 기존 T11 Proposal 스키마, run/source, 허용 편집 경로와 원본 파일 hash를 다시 검사한다.
`schema_valid`·`lint_passed`는 실제 boolean `true`여야 한다. 숫자 `1`을 성공으로 받지 않는다.
실제 T11 검사 결과가 없는 adapter가 이 값을 만들어서는 안 된다.

이는 T10 내부 계약 구현이며 운영 연결 완료가 아니다. T11 최초 입력 변환·검사 계약,
T38 전용 intent/영수증 adapter, 실제 T16 Collection/Analysis, T7 발행 조회기,
T15 저장 제안 소비는 각 담당의 정본으로 연결해야 한다. REHEARSE용 `FailureBundle`이나
Admission을 최초 입력으로 꾸미지 않는다. 후보 이미지가 아직 없으면 PROPOSE에 남는다.

## 기존 생산자와의 관계

[T15 #191](https://github.com/Softbank-Hackathon-Finals/platform/pull/191)의
`9b9681ab30eeae5ba72573d71784605f401d705a`를 대조했다. 그 러너의
`RECEIPT_UNVERIFIED`는 아직 인증하지 않은 검증 응답이다. 테스트의
`FakeReceiver`와 `verified=True`도 운영 승인 근거가 아니다.
이 객체를 `CurrentChange`로 자동 변환하는 어댑터는 설치하지 않는다.

T15/T38 결과 공급자는 인증된 원문, 현재 시도·제안·검증한 제품 이미지·대상,
정리 완료와 취소·보안 hold를 확인한 뒤 `CurrentChange`를 반환해야 한다.
diff와 image digest를 계산하거나 검증하는 방식은 생산자 계약을 따른다.
`verification_id`는 같은 계획에서 재사용하지 않는 불변 검증 참조다.
현재 기본 브랜치와 #191에는 이 운영 공급자가 없으므로 기본 동작은 거부다.

## 데이터와 권한

| 값 | 출처·용도 |
|---|---|
| `binding` | 기존 [이벤트 스키마](../../services/platform-events/src/bravo_platform_events/schemas/event.schema.json)의 `change` 승인 형식. plan·diff·image digest·대상·만료를 재사용 |
| `rehearsal_id`, `verification_id` | 서버가 확인한 현재 리허설과 불변 검증 참조. 요청 본문에서 증거로 받지 않음 |
| `revision` | 검증·정리·현재 산출물·취소 상태가 달라지면 바뀌는 공급자 revision |
| `approve_change` | 변경을 승인할 현재 사람 권한 |
| `apply_change` | 승인된 변경의 반영을 접수할 현재 사람 권한 |

사람 인증·계획별 권한 부여·회수는 기존 [운영자 접근](T10-operator-access-v1-proposal.md)을 쓴다.
새 권한도 `read`를 함께 요구한다. S3의 `approve_cutover`·`execute_cutover`가 S2 권한을 부여하지 않는다.
실제 사람에게 권한을 부여하거나 IdP를 구성하는 작업은 포함하지 않는다.

## 승인 요청

`POST /v1/plans/{plan_id}/approvals`에 `approval_kind="change"`, 승인 화면에 표시한
리허설 이벤트의 `run_id`, `binding`,
`expected_version`과 `Idempotency-Key`를 보낸다. 호스트가 `change_service`를
명시적으로 연결했을 때만 받는다. S2 응답은 `run_id`를 돌려준다.
계획 안에서 리허설 두 건의 diff·이미지가 같아도 서버는 지정한 실행의 결과를 조회한다.

계획 잠금 뒤 현재 사람·tenant·read 권한, `REHEARSE` 상태, 계획 버전과 현재
공급자 결과를 확인한다. 공급자 조회를 마친 시각에도 승인 만료와 사람 권한을 확인한다.
요청의 결속 값이 현재 값과 같아야 승인·`approval.decided` 이벤트가 한 트랜잭션에 저장된다.
내부 검증 참조·권한 revision은 공개 응답에서 제외한다.

두 승인 종류는 같은 HTTP endpoint의 멱등 키 공간을 공유한다. 같은 키로 종류나
입력을 바꾸면 거부한다. 같은 요청의 재조회는 저장된 승인 영수증을 돌려주며,
그 영수증을 돌려받았다는 사실이 현재 실행 권한을 뜻하지는 않는다.

## 반영 접수와 재시작

`ChangeApprovalService.claim_application`은 신뢰된 서버가 호출하는 내부 접점이다.
`apply` HTTP 경로나 T31 자격증명을 추가하지 않는다.

1. 같은 계획 잠금에서 실행자 권한, 원 승인자의 현재 권한과 revision, 승인 취소,
   현재 T15 검증·결속·만료, 계획 버전·`REHEARSE` 상태를 다시 읽는다.
2. 승인 소비와 반영 접수 영수증을 함께 저장한다. 같은 리허설·검증을 다시 승인해도
   두 번째 반영을 접수하지 않는다.
3. commit이 확인된 첫 호출만 `(receipt, True)`를 받는다. 재시작·동일 요청 재시도는
   `(saved_receipt, False)`다. 후자는 원격 효과를 재전송할 권한이 아니다.

T31 연결자는 `application_id`로 원격 실행을 재조회할 수 있는 영속 실행 경로를
별도로 제공해야 한다. commit 응답 손실이나 접수 뒤 프로세스 중단 시, 그 경로가
확인되기 전에는 재배포하지 않는다. 이 PR은 T31 접수 API나 복구 계약을 정하지 않는다.
반영을 접수한 계획은 `REHEARSE`에 남는다. T31의 실제 배포 결과 공급과 T19 게이트
재대조 호출은 별도 연결이 필요하다.

## 배포 결과를 읽은 뒤 상태 복구

T31이 보고한 배포 결과로 `REHEARSE`에서 `GATE`로 넘어갈 때, T10은 같은 계획 잠금에서
변경 승인과 반영 접수 영수증을 읽는다. 현재 계획 버전에 발급한 승인인지 확인하고,
배포 결과의 반영 ID·검증 ID·diff·이미지 digest를 접수 영수증과 대조한다.
제안의 변경과 실제 배포 digest도 일치해야 한다. 승인이나 접수 내역이 없거나 값이
다르면 계획은 `REHEARSE`에 머문다. 재시작 시에도 동일한 저장 기록을 사용한다.

## 저장과 오류

기존 `plan_commands`·이벤트·SQLite `BEGIN IMMEDIATE`·PostgreSQL 계획 advisory lock을 쓴다.
테이블·DB 역할·마이그레이션은 추가하지 않는다. 상태를 바꾸거나 자격을 회수하는 모든
서버 작성자는 같은 계획 잠금 규칙을 따라야 한다.

| 응답 | 뜻 |
|---|---|
| 403 | 확인되지 않은 사람 또는 현재 권한 없음 |
| 409 | 현재 결속·버전·상태 불일치, 만료, 멱등 키 충돌 |
| 422 | 잘못된 요청 형식, 호스트가 연결하지 않은 승인 종류 |
| 503 | 현재 검증 공급자를 확인할 수 없음 |

[T10 작업 계획](../tasks.md)의 완료 기준은 목업 단계의 `COLLECT → DONE`과
조건 미충족 전이 거부다. S2·S3 승인과 현재 계획·diff·digest·만료의 거부 시험을
그 기준으로 대조한다. 실제 T15 영수증·T31 반영·운영 S3 연결은 별도 통합
인수이며, 목업 통과를 실제 배포나 해피패스 완료로 표시하지 않는다.

## T15 수신과 같은 검증 ID의 hold

호스트가 `ChangeIntake`에 T15 전용 `WorkloadSender`와 인증 원문 조회기를
주입한 경우의 내부 계약이다. 발신 JWT는 고정한 공개 RSA 키(RS256), issuer,
단일 audience, subject, 유효기간(최대 1시간)으로 확인한다. tenant는 배포 설정에서
고정한다. 수신 요청으로 키나 URL을 고를 수 없다. 사람의 승인은 별도 운영자
경로를 거친다. 키 교체 시 호스트가 신뢰 설정을 갱신해야 한다.

1. `receive(token, candidate)`는 발신 인증과 계획의 tenant를 확인한 다음,
   계획·실행·검증 ID와 불투명한 원문 참조만 조회기에 전달한다. 조회기는 인증한
   불변 원문과 **실제로 확인한 산출물**에서 후보를 재구성한다. 검증을 시작한
   계획 버전은 신뢰된 실행 기록에서 읽는다. 수신 시점의 최신 버전을 대신 쓰지 않는다. 전송된 후보를
   복사하거나 `PASS` 문자열만 보고 원문을 만들면 안 된다.
2. T10은 후보의 모든 필드, 원문의 tenant, PASS, 생성·삭제 UID 일치와 부재 확인을
   대조한다. 계획 잠금에서 토큰을 다시 검증하고 계획 버전·digest·목적지·상태·만료를
   확인한다. 불변 검증 기록과 실행별 최신 시도를 한 번에 저장한다. 같은 ID의
   같은 원문 재전송은 기존 수신 확인만 반환한다. 같은 ID의 다른 원문과
   과거 시도의 신규 등록은 거부한다. 재전송은 현재 후보를 바꾸지 않는다.
3. 취소·보안 담당의 별도 권한 정책이 같은 계획 잠금에서 호출자와 결정 참조를
   확인한 뒤 `hold`를 기록한다. 미수신 ID에도 먼저 기록할 수 있다.
   같은 ID의 hold는 영구적이다. hold를 해제하는 API는 없으며 새 검증에는
   새 ID가 필요하다. 다음 시도 시작·산출물 교체·정리 상태 변경 때에는 생산자가
   **이전 검증의 hold 커밋을 먼저 확인**해야 한다. T10은 통보되지 않은 외부
   상태 변경을 알 수 없다.
4. 기존 운영자 호스트의 `change_authority=intake.current` 접점으로 연결한다.
   승인 또는 반영 접수는 같은 DB 연결에서 계획 버전·최신 시도·hold를 함께 읽는다.
   같은 계획이 다음 버전의 리허설로 돌아와도 전 회차 검증으로 새 승인할 수 없다.
   hold가 먼저 커밋되면 새 결정은 거부한다. 접수가 먼저 커밋되면 동일 요청은
   저장한 ID와 `newly_created=False`만 돌려준다. hold는 이미 접수한 원격
   작업을 되돌리지 않는다.

현재 배포에는 이 접점을 열지 않았다. 실제 발급자·공개 키/교체·audience·subject,
인증 원문 저장소와 산출물 조회기, 취소·hold 권한 정책은 운영 연결의 선행 조건이다.
합성 예시의 `evidence_ref`는 제품 증거가 아니다. 이 변경으로 T15·T38 제품
Job이나 T31 배포가 실행되지 않는다.

### 별도 T15 프로세스에서 후보를 전달할 때

호스트가 `create_operator_cutover_app(..., change_intake=intake)`를 지정하면
`POST /internal/v1/rehearsal-candidates`를 연다. `intake`는 API와 같은 저장소를
사용해야 한다. 이 설정은 S2 authority를 `intake.current`로 함께 연결한다.
다른 `change_authority`와 동시에 지정할 수 없다. 기본 설정에는 이 경로가 없다.

- 인증: T15 전용 `Authorization: Bearer <workload JWT>`. 사람이 승인하는
  토큰·권한과 다르다. 수신기는 인증 뒤 원문을 읽고, 계획 잠금 뒤 만료를 다시 확인한다.
- 본문: `Candidate`의 필드만 받는다. PASS·원문·UID 정리 증명·hold·tenant를
  본문에 추가하면 거부한다. 원문은 호스트가 주입한 조회기에서 독립적으로 읽는다.
- 성공: HTTP 200과 `{verification_id, attempt, revision}`만 반환한다.
  같은 후보의 재전달도 같은 수신 확인이다. 사람 승인이나 배포 성공이 아니다.
  이 경로에는 hold·apply·권한 부여 기능이 없다.

T15의 기존 `submit_candidate`에 `HttpChangeIntake`를 전달할 수 있다.
생산자의 `CandidateSubmission.t10_fields()`와 수신 `Candidate` 형식은 유지한다.
클라이언트는 호스트가 고정한 HTTPS origin과 검증된 CA만 사용한다.
환경 프록시·리다이렉트·자동 재전송을 사용하지 않는다. 토큰은 호출마다 공급한다.
인증 실패·응답 유실·잘못된 수신 확인은 `IntakeDeliveryUnknown`이며,
리허설을 다시 실행하거나 승인으로 올리지 않는다. 운영자가 필요하다고 판단하면
같은 후보를 다시 전달할 수 있고, 수신기의 기존 불변 기록을 재사용한다.

서버는 TLS를 직접 종료하거나 신뢰한 프록시의 scheme만 받아야 한다.
임의 `X-Forwarded-Proto`는 인정하지 않는다. 실제 issuer·키 교체·audience·subject,
원문 보존 경로와 읽기 mount, 별도 hold 권한은 배포 전에 확정해야 한다.
이 접점의 로컬 HTTP/DB 시험은 실제 T15 planner·T31 실행 연결을 대신하지 않는다.

배포 설정의 검증과 전용 projected token은
[T15 발신 신원 설정](../../services/platform-events/deploy/intake/README.md)을 따른다.
공개 키의 오프라인 검증과 Pod·SA 삭제의 즉시 철회는 구분한다.
T15 HMAC 키를 별도 T10 Pod에 복제하지 않는다. 인증 원문 조회와 hold 관리
주체를 실제로 연결하기 전에는 수신 경로를 열지 않는다.

같은 trusted host 구성은 [승인 호스트 실행](../../services/platform-events/deploy/approval/README.md)을
따른다. 기존 `build_bucket_host`와 `RecordedEvidence`가 같은 기록 저장소를
사용한다. 실행은 요청 밖의 단일 dispatch 작업자에서 진행하며 자동 재시도하지
않는다. 취소·보안 hold는 별도 `hold_rehearsal` 사람 권한을 같은 계획 잠금에서
확인한다. 권한은 기존 관리 경로에서만 등록하며 workload에 부여하지 않는다.

## 리허설을 시작하고 재조회하기

운영자 호스트에 서명 신원 검증기와 T15 어댑터(`rehearsal_host`)를 연결한다. 운영자는
계획별 `read`와 `start_rehearsal` 권한이 있어야 한다. 다음 요청에서 T10은 계획 잠금을
얻은 뒤 권한, 현재 `REHEARSE` 상태와 버전, 서버가 해당 계획에 등록한 작업과 소스 revision을 확인한다.
`task_digest`는 같은 계획 버전의 논리 작업을 식별하므로 시도 간 유지된다. `proposal_digest`는
각 시도의 수정안을 식별하므로 다음 시도에서 달라질 수 있다.

```http
POST /v1/plans/{plan_id}/rehearsals
Idempotency-Key: start-bucket-1
Content-Type: application/json

{"task_id":"bucket","expected_version":4,"attempt":1}
```

`202` 응답에는 `task_id`, `plan_version`, `rehearsal_id`, `attempt`가 있다.
접수와 실행 의도는 하나의 DB 트랜잭션에 저장되고 T15 호출은 커밋 뒤 한 번 일어난다.
같은 계획 버전과 같은 작업·시도에 다른 키로 요청해도 실행 ID는 같다. 다른 작업은
별도의 ID를 받는다. 프로세스가 커밋 뒤 중단되거나 제출 응답이 유실되면 같은 키로 영수증을
조회한 뒤 아래 경로로 원격 결과를 대조한다. 새 namespace를 만들 목적으로 재송신하면 안 된다.

```http
POST /v1/plans/{plan_id}/rehearsals/{rehearsal_id}/attempts/{attempt}/reconcile
```

T15 어댑터가 같은 실행·시도에 결속된 원본을 찾아야 한다. 원격 효과를 확인할 수 없으면
`UNCONFIRMED`로 두고 운영자가 조사한다. 확정 판정에는 인증된 검증 ID, 제안 digest,
namespace, 생성·삭제 UID 일치와 부재 확인이 필요하다. PASS에는 T10이 받은 그 시도의
검증 원문도 필요하다. FAIL 뒤 다음 시도는 해당 검증 ID의 보류가 커밋된 후에만
가능하며, 실행 ID를 유지하고 시도 번호만 올린다(최대 3회). 같은 계획에서 시작한
작업 중 하나라도 완료되지 않았거나 보류된 경우 `REHEARSE → GATE` 전이가 거부된다.

실제 T15 연결 시 실행 ID·시도별로 Job과 namespace 및 UID를 다시 찾을 수 있는
제출·조회 어댑터가 필요하다. 리허설 접수 영수증을 사람의 변경 승인이나 배포 결과로 쓰지 않는다.

기존 방식으로 검증 후보나 승인이 이미 저장된 계획 버전은 기존 절차로 마친다.
새 방식으로 첫 접수를 기록한 버전은 T10이 발급한 실행 ID와 시도만 받는다.
재조회가 돌려주는 PASS는 그 시도의 과거 결과다. 이후 보류가 기록되면 같은 PASS 영수증이 있어도
승인·반영과 다음 단계 전이는 현재 보류를 다시 읽고 거부한다.

호스트 활성화 순서는 전체 T10 쓰기 프로세스 교체 후 `rehearsal_host` 연결이다.
기존 버전의 상태 전이·수신·승인 프로세스는 새 접수 기록을 해석하지 않는다. 교체할 때는
새 접수를 중지하고 기존 writer의 종료를 확인한다. 새 방식으로 시작한 계획은 해당 버전의
승인·반영과 전이가 끝날 때까지 새 가드를 이해하는 writer가 맡아야 한다.

### T38 제품 제안 입력 등록

`ProductInputRecords`는 trusted host 내부 포트다. 기존 `plan_commands`에 입력을
한 번 저장하며 새 HTTP 경로나 DB 스키마를 만들지 않는다. 설치 그룹은
`uv sync --group product-inputs`다. 계약 검증은 T38의 모델 실행과 분리된
`bravo_agent.product_contracts`를 사용한다.

호스트는 현재 catalog, 승인된 불변 원본을 읽는 `source`, 현재 서비스 principal의
등록·tenant·계획·purpose 권한을 검사하는 `authorize`를 필수로 제공한다.
`source(connection, plan, admission, task)`는 `bundle`, `manifest`, `files`만 반환한다.
요청 본문·모델 출력·임의 로컬 경로를 원본으로 연결하지 않는다.

버킷 작업에는 `BucketSnapshotSource`를 연결한다. 호스트가 인증한 T11 `Snapshot`과
고정된 source/target 정책 경로를 받는다. catalog의 정책 원문과 source SHA를 대조하고,
T11 읽기 검증을 거친 두 파일만 기존 T15 `bucket_product_bundle`로 전달한다.
수정 범위는 승인된 target 정책 한 파일로 제한한다. snapshot 수집·등록·mount를
자동으로 만들지 않으며, 다른 작업 종류에는 해당 담당의 승인된 source 포트가 필요하다.

1. T15 소비 어댑터의 `product_input(admission, task)`는 커밋된 Admission 8필드,
   현재 `REHEARSE` 상태·버전·tenant와 catalog 논리 작업을 같은 계획 잠금에서 확인한다.
   T10이 Admission 전체 digest와 시도를 붙인다. 원본 파일은 T38 정본 검증을 통과해야 한다.
2. 호스트의 `register(admission, value, principal=..., expires_at=...)`는 승인된
   원본과 입력 전체 digest를 다시 대조한다. 첫 시도에 불투명 `input_ref`와
   `input_digest`를 발급한다. 같은 Admission·입력·호출자·만료는 같은 참조를 받는다.
   다른 내용이나 만료 연장은 거부한다. 참조 자체는 읽기 권한이 아니다.
3. 인증된 broker 조회 포트 `read(principal, input_ref)`는 현재 계획·catalog·권한·
   회수·만료를 다시 검사한다. 반환 필드는 `admission`, `product_input`, `input_digest`,
   `principal`, `purpose`, `expires_at`의 6개다. `purpose`는 `t38-product-proposal`,
   만료는 UTC다. `revoke`는 신뢰한 관리 경로에서만 호출하며 회수는 영속적이다.

`principal`은 T38 STS가 검증한 issuer/audience/subject의 digest다. 사람의
`OperatorPrincipal`이나 T10 workload JWT와 다르다. `register` 결과는 T15
`request_for`의 참조/digest 부분이며, `web_identity_token`은 별도의 기존 GitHub
OIDC 공급 경로에서 넣는다. 이 저장소에는 토큰을 넣지 않는다.

이 포트는 첫 모델 제안만 다룬다. 시도 2·3은 등록 전에 거부한다. T15의 논리
`Admission.task_digest`와 T11 `FailureBundle` digest는 서로 다른 계약이다.
운영 source collector·서비스 principal 등록·인증된 broker 읽기 transport·OIDC
공급을 실제로 연결하기 전에는 제품 실행 경로가 완성되지 않는다. 로컬 입력 등록과
조회 성공은 모델 응답·검증 Job·사람 승인·T31 배포의 완료 근거가 아니다.

현재 grant의 append-only 관리와 기존 Actions OIDC를 사용하는 사설 read HTTP는
[제품 입력 조회 서비스](../../services/platform-events/deploy/approval/PRODUCT-READ.md)를 따른다.
이 서비스는 사람 IdP 없이 schema 2/3에서 조회만 제공한다. 별도 승인 호스트의
schema 3 요구·사람 승인·단독 실행 조건은 유지한다.
