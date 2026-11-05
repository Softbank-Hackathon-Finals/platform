# ADR-0028: 최초 제안을 PROPOSE 단계에서 접수하고 저장한다

- 날짜: 2026-11-06
- 상태: T10 경계 결정. T11·T15 연결 계약과 운영 인수는 별도.
- 근거: [최초 제안 분담과 지환 측 결정](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793887642765649), [T10 #19](https://github.com/Softbank-Hackathon-Finals/platform/issues/19).

## 배경

기존 ProductInputRecords는 REHEARSE의 실제 Admission을 요구한다. 그런데
PROPOSE에서 REHEARSE로 넘어가려면 이미 Proposal과 후보 이미지가 있어야 한다.
이 리허설 경로를 최초 모델 호출에도 사용하면 서로의 결과를 기다리게 된다.
기존 작업의 grant·입력 검사를 완화해서 이 순환을 풀면 단계 권한이 섞인다.

## 결정

T10에 최초 PROPOSE용 입력·단일 요청·결과 접점을 둔다. 기존 계획 잠금과
plan_commands, TaskCatalog, Proposal 스키마, WorkflowRecovery를 재사용한다.
현재 목적 권한은 별도 grant로 확인한다. T38은 한 번만 전달하고 같은 요청 결과를 조회한다.
T10은 인증된 정규화 결과를 저장하며, T15는 같은 저장 제안을 소비한다.

실제 T7 후보 발행 영수증이 있을 때만 diff/image digest를 Change에 결속한다.
상세 필드·호출·실패 의미는 [기존 변경 계약](../contracts/T10-change-approval.md#최초-propose-접수와-같은-제안의-소비)과
구현 모델을 정본으로 관리한다. 이 ADR에 스키마를 복제하지 않는다.

## 결과와 한계

가짜 CollectionProof·Analysis·Admission과 임의 이미지 digest를 넣을 필요가 없다.
최초 요청을 접수한 뒤 응답이 없어도 모델을 다시 호출하지 않는다. 접수 commit 뒤
프로세스가 전송 전에 중단된 경우도 read-only 복구하며, 미실행을 추측해 재전송하지 않는다.

T10 로컬 회귀는 SQLite/PostgreSQL의 저장·권한·전이 경계를 검증한다.
실제 모델 성공이나 전체 E2E 완료를 뜻하지 않는다. T11 모델 입력과 검사 결과,
T38 인증된 실행 adapter, T7 발행 조회기, T15 소비는 각 담당 범위에서 연결한다.
