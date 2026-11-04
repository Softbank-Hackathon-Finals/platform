# T10 S2 변경 승인과 반영 접수

설계 [S2](../design.html#s6-2)·[상태 머신](../design.html#s8-2)·[API](../design.html#s8-9)의
변경 승인을 기존 계획 저장소에 연결한다. T15가 검증한 변경에 사람이 승인하고,
반영 직전에 같은 변경인지 다시 확인하는 T10 경계다.
T15 검증·재시도, T19 판정, T31 배포를 이 모듈에서 실행하지 않는다.

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

실제 T15 영수증 공급과 T31 실행 연결 전까지 이 기능만으로 S2 완료나 T10 전체
승인·재시작 완료를 체크하지 않는다. SQLite/PostgreSQL의 합성 시험과 운영 인수는 구분한다.
