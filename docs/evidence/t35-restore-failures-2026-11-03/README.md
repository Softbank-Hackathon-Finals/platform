# T35 복원 실패 원인 실측 — 2026-11-03

기존 정상 원본과 정상 복원본이 있는 작업 전용 kind에서, 서로 다른 새 CNPG 복원 Cluster 3개를 만들었다. 원본/기존 reader ObjectStore를 수정하지 않았고, 시험 전후 UID·spec 해시 및 원본·정상 복원본의 Ready를 대조했다.

| 결함 | 관측한 거부 | 판정 범위 |
|---|---|---|
| 존재하지 않는 고정 백업 ID | `no backup found with ID` + 해당 ID, 복원 프로세스 오류 종료 | 요청한 백업이 없으므로 복원 거부 |
| 저장소 인증서와 다른 CA | Barman `CERTIFICATE_VERIFY_FAILED`, 복원 프로세스 오류 종료 | 인증서를 건너뛰고 복원하지 않음 |
| 별도 Secret의 가짜 저장소 자격 | `InvalidAccessKeyId`, 복원 프로세스 오류 종료 | 자격을 우회하여 복원하지 않음 |

세 시험 모두 Ready가 아니었다. 단순히 Ready가 아니라는 사실만으로 합격 처리하지 않았다. 첫 임시 수집기는 백업 없음 문구를 놓쳐 `qualified_failure: false`를 기록했다. 이후 정확한 오류 필드와 Pod→Job→Cluster 소유권, 실패한 복원 프로세스를 대조하는 읽기 전용 검증기로 다시 확인했다. 최초 기록은 비공개로 보존했다.

- CNPG 1.30.1, Barman Cloud plugin 0.15.1, PostgreSQL 18.4, SeaweedFS 4.52, Kubernetes 1.34.
- 실제 로그·자원 UID·설정은 접근 제한된 비공개 원본에 보존한다. 공개 결과는 원문 전체가 아닌 제한된 결과 projection이다.
- `result.json`의 로그/설정 해시는 정확한 비공개 원본을 대조하기 위한 것이며 공개본만으로 AWS/RKE2 실행을 증명하지 않는다.
- 검증 도구: [읽기 전용 복원 실패 자격 시험](../../../services/control-runner/qualification/README.md).

**미검증:** 팀 AWS S3 및 실제 목적지 RKE2, 장애 복구 시간 보장, WAL 손상/누락, PITR, 실제 서비스 데이터의 복원, 백업 보존 정책과 관리자 삭제 방어. 이번 결과는 정상 복원·표식 행 확인 또는 DR-01 PASS를 대신하지 않는다.
