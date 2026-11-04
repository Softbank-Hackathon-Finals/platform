# T35 새 복원 대상과 SQL 읽기 결속 검증

2026-11-04 KST. 기존 소유 로컬 kind/CNPG 시험 환경에서 새 표식을 기록하고,
그 이후의 새 Barman 백업을 생성해 별도 UID의 클러스터로 복원했다.
원본은 업무용 DB가 없는 전용 시험 DB이며 표식 기록 시 선행 쓰기를 잠금으로 drain했다.
표식 이전 행을 수정·삭제하지 않는 제한된 경로다.

## 실제 확인

1. 백업 요청 전에 보존한 source/writer ObjectStore 지문과 인증된 create 응답 UID를
   `CNPGBackupReader`가 완료 상태와 대조했다.
2. 정확한 backup ID와 읽기 전용 reader-store로 새 클러스터를 생성했다.
   생성 응답의 기본값 적용 spec/UID와 생성 전 reader-store 지문을 보존했다.
3. `CNPGRestoreReader`가 Cluster·ObjectStore·Ready·primary를 SQL 전후로 확인했다.
4. Pod ownerReference UID로 대상 primary를 확인해 터널링하고 대상 CA로
   `verify-full` 연결했다. 시험 역할은 SELECT만 허용했다.
5. `RestoreProbe`에서 원본 표식 해시·ID·행 수와 실제 복원 조회가 일치했다.
6. 같은 등록 복원 대상을 새로 읽는 후속 시험에서 SELECT 회수, 잘못된 CA,
   다른 target UID와 backup 영수증을 거부하고 권한 복구 후 다시 성공했다.

`result.json`은 식별자를 제거한 결과와 비공개 영수증 해시다. 원문·접속 비밀은 공개하지 않는다.
최종 구현 기준 관련 단위 시험 31개, control-runner 전체 420개·58개 subtest 통과,
환경 조건부 4개 제외. SQL 실환경 시험은 이 제외 항목을 PASS로 바꿔 센 것이 아니다.

## 보존·한계

기존 source·두 복원 클러스터를 삭제하거나 재구성하지 않았다. 이 시험의 새 백업과
새 복원 대상도 조사용으로 보존했다. 새 대상의 시험 전용 역할 외 운영 권한은 바꾸지 않았다.
명시한 소유권과 UID를 재확인한 별도 정리 절차가 필요하다.

팀 EKS/RKE2 연결, 운영 실행 원장, 일반 앱, 전체 행 내용 검증, PITR, RTO/SLO,
실패 도메인 분리 또는 금융 보안 인증의 완료 근거로 사용하지 않는다.
세부 신뢰 경계는 `services/control-runner/docs/cnpg-readback.md`를 따른다.
