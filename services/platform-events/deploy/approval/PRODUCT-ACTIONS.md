# 같은 Admission의 Actions 실행과 결과 조회

`ProductExecution`은 T10의 커밋된 Admission과 불변 제품 입력을 한 Actions 실행
의도에 결속한다. T38의 순수 `ExecutionIntent`와 결과 검증기를 사용한다.
실행 의도는 기존 `plan_commands`에 저장하며 새 스키마는 추가하지 않는다.

## 단일 발행과 현재 권한

호스트는 기존 `ProductInputRecords`로 입력을 등록한 뒤 `execute(admission,
input_ref=..., input_digest=...)`를 호출한다. 같은 계획 잠금에서 현재 plan·tenant·
catalog·grant·만료·회수를 확인하고, principal과 실행할 정확한 platform SHA까지
결속한 의도를 먼저 커밋한다. 최초 의도를 만든 호출만 `client.submit(intent)`를
한 번 호출한다. 같은 Admission의 참조·digest·runtime SHA를 교체하면 거부한다.

T38의 `OperatorProductExecution`은 기존 운영자 `gh` 인증으로 워크플로를 발행한다.
이 객체를 Kubernetes Pod에 넣고 운영자 자격을 복사하는 배포 방식은 제공하지
않는다. 운영자 호스트의 경계와 기존 등록·배포 입력을 먼저 인수해야 한다.

재시작·동시 요청·응답 유실 뒤에는 `client.read(intent)`만 호출한다. 의도를
커밋한 직후 발행 전에 프로세스가 사라진 경우도 다시 발행하지 않는다. 운영자는
기존 의도와 실제 GitHub 실행을 대조한다. 결과가 불명이면 새 실행을 보내지 않는다.

## 결과 소비와 제한된 대기

T38 클라이언트가 실제 GitHub run·actor·workflow·artifact를 대조한 envelope만
반환한다. T10은 같은 요청·Admission·입력·runtime SHA·attempt 1·정리 완료를
공유 검증기로 확인한다. 원래 bundle·snapshot도 대조하고, 결과를 저장하기 전에
현재 권한을 다시 검사한다. 저장 응답이 유실돼도 같은 원격 결과만 재조회한다.

`ActionsProductCall`은 토큰 없는 `{input_ref,input_digest}`만 받는다. 최초 실행이
`ExecutionPending`이면 정해진 시간 동안 같은 의도의 `read`만 호출한다. 기본은
900초, 조회 간격은 5초다. 호스트 설정은 1–1800초와 0.1–30초 범위로 제한한다.
`ExecutionUnknown`이나 기한 초과는 확인 불가로 멈춘다. 새 workflow·새 모델 호출을
자동으로 보내지 않는다. T15에는 확인된 기존 제품 응답만 `(200, result)`로 전달한다.

T15 `RemoteProposalSource`의 참조 전용 요청 포트가 공식 담당 범위에서 인수된 뒤
이 호출부를 연결한다. `request_for`는 기존 입력 등록의 ref/digest를 돌려주고,
실제 OIDC와 `ProductRequest` 조립은 Actions 안에서 수행한다. 가짜 token 필드로
기존 세 필드 계약을 통과시키지 않는다. 승인된 입력 유효기간은 최초 등록에
고정하며 재조회 때 현재 시각으로 늘리지 않는다.

이 결과는 모델 제안까지의 근거다. T15의 현재 Coordinator·검증 Job·UID 정리와
기존 서명 기록, T10의 원본 조회·변경 승인 대기를 그대로 거쳐야 한다. T15 프로세스
재시작이나 완료 여부가 불명인 시도를 임의로 다시 여는 기능은 추가하지 않는다.
