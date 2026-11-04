# T35 상태 조회 최소권한 실측 — 2026-11-03

기존 전용 로컬 kind/CNPG 시험 환경에서 매 실행 새 ServiceAccount·Role·RoleBinding을 만들고
10분 TokenRequest 토큰으로 Kubernetes API를 직접 호출했다. 인증서는 검증하고 프록시·HTTP
리디렉션을 사용하지 않았다. 토큰·원본 kubeconfig는 파일이나 결과에 기록하지 않았다.

| 동작 | 실제 결과 |
|---|---|
| 지정 Cluster·Backup·ObjectStore GET + CNPGBackupReader | 완료 영수증 반환 |
| Cluster/Backup 목록, 다른 Backup, Secret, Pod, 다른 namespace Pod 조회 | API Status Forbidden, HTTP 403 |
| Backup 생성·삭제, Cluster 수정(dryRun=All) | API Status Forbidden, HTTP 403 |
| RoleBinding 회수 후 같은 토큰으로 읽기 | 거부, 완료 영수증 없음 |
| RoleBinding 복원 후 같은 토큰으로 읽기 | 정상, 이전 영수증과 동일 |

총 9가지 권한 거부를 확인했다. timeout·401·404를 거부 성공으로 세지 않았다.
백업/DB 변경 시험은 서버 dry-run으로만 수행했다. 실제 백업·Cluster·ObjectStore를 변경하거나
삭제하지 않았다. 임시 RBAC 자원은 생성 응답 UID를 DeleteOptions precondition으로 지정해 정리했다.

검증기 구현은 PR #120의 `5563ec6b7940aa5093108e83c2a1f71aee152b62`를 사용했다.
새 manifest 회귀 시험 37개, 전체 control-runner 378개·subtest 58개 통과.
소스 SHA와 공개 가능한 결과는 `results.json`, 요청/완료 영수증과 원문 증거는 로컬 비공개 보존.

이 시험은 상태 조회자의 Kubernetes 권한이다. 앱의 DB 권한·스토리지 객체 삭제 제한·실제 복원 데이터
검증·팀 AWS/RKE2 적용을 대체하지 않으며 T35 전체 완료를 뜻하지 않는다.
