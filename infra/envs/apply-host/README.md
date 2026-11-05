# T31 실행 호스트의 영속 기록

T10의 `ApplicationHost`가 승인 접수를 먼저 저장하고 T31의 `apply`를 호출한다.
같은 접수는 `get_result`만 호출한다. 접수 후 실행 전 중단되어 기록이 없어도 재반영하지 않는다.
기존 T31 구현과 T10 상태 머신은 변경하지 않는다.

`render.py --node <현재 플랫폼 노드>`는 `bravo-apply-control`의 전용 PVC와
`/var/lib/bravo-t31` local PV만 생성한다. 설치자는 대상 클러스터 UID, 기존 namespace,
`platform-local` StorageClass, 현재 노드, 충돌하는 PV/PVC와 경로 부재를 먼저 확인한다.
호스트에서 전용 경로를 uid/gid 10001, mode 0700으로 만들고 서버 dry run 뒤 적용한다.
DB의 별도 EBS나 `/var/lib/bravo-platform-data/postgres`는 사용하지 않는다.

실행 Pod는 PVC를 `/var/lib/bravo/t31`에 mount하고 그 아래 `applications`를 사용한다.
마운트가 없으면 시작하지 않는다. `ApplicationHost` 인스턴스는 프로세스 수명 동안
유지한다. 단일 replica와 Recreate 정책에 더해 파일 잠금으로 중복 실행 호스트를 거부한다.
PVC의 RWO만으로 단일 프로세스를 보장하지 않는다. 기록 볼륨은 삭제/초기화하지 않는다.

신뢰된 호스트 구성에는 다음 입력이 모두 필요하다.

- 현재 권한과 T15 원본을 검증하는 `ChangeApprovalService`, 등록된 목적지와 승인 후보를 읽는 `load_input`.
- 대상 전용 `apply-deployer` 자격을 보호 mount로 받는 `KubectlKube`. 토큰을 argv나 로그에 넣지 않는다. 짧은 수명 자격의 회전은 별도 인수해야 한다.
- `GateRecords`의 현재 권한 정책과 T19의 원본·권한·유효기간 제공자. `GateRecheck.prepare`는 반영 후 실제 대상 통제 시험을 수집·결속한다. 미수신 시 실패해야 한다.
- `ApplicationEvidence.check_current`는 현재 권한·hold·등록 대상·인증된 실행 digest를 대조한다. `load_gate`는 동일 plan 트랜잭션에서 T19 service/readiness를 읽는다.

T10은 `ApplicationEvidence`를 기존 `WorkflowRecovery.read_evidence`에 연결한다.
APPLIED를 소비하면 GATE로 이동한다. 이때 `GateRecheck.evaluate`를 호출하고,
저장한 원본을 다시 읽는 `resume_once`가 별도 T19 PASS를 확인해야 AWAIT_APPROVAL로 이동한다.
요청된 세 통제의 목록은 PASS가 아니다. 대상 run 시작·각 증거 수집 시각이 반영 완료보다
이전이면 거부한다. 기존 유효한 재대조는 `prepare`가 application_id로 재사용한다.

`probe.py`는 서로 다른 Pod에서 보존된 합성 marker와 파일 잠금만 확인한다.
대상 자격·승인·반영·T19 PASS는 만들지 않는다. 실제 실행에는 위 신뢰된 입력과
새 T15 검증/S2 승인이 필요하다. 이 저장소 검사는 운영 반영 완료 증명이 아니다.

local PV는 기존 단일 노드 root EBS에 의존한다. 선언한 1Gi는 디스크 quota가 아니다.
Pod 재생성 보존만 제공하며 노드 손실 복구, HA, 별도 백업을 의미하지 않는다.
용량 감시와 백업은 운영 인수에 별도로 기록한다. 새 노드나 유료 볼륨은 만들지 않는다.
