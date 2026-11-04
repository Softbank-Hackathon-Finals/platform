# T35 최초 복구 WAL 누락 거부 — 2026-11-04

정상 백업이 있는 작업 전용 kind/CNPG·SeaweedFS에서, 별도 prefix에 base 백업 파일
두 개만 복사하고 WAL은 복사하지 않았다. 원본 archive를 훼손하지 않았다.

- 정상 백업 메타데이터의 `begin_wal`과 Barman의 최초 필요 WAL 누락 오류를 대조했다.
- Cluster UID·spec·backup ID, Pod→Job→Cluster 소유권, 실패한 recovery 프로세스를 확인했다.
- 원본 백업의 ETag·크기와 원본 DB UID·spec·Ready, 기존 정상 복원본 Ready가 유지됐다.
- 새 시험 Cluster·ObjectStore는 UID precondition으로 정리했다. 원본 삭제 제한을 유지하므로 복사한 base 파일은 로컬 저장소에 남겼다.

최초 일반 상태 수집에서는 Ready 아님만 확인되어 원인이 미확인이었다. 실제 오류 형식을
확인한 뒤 명시적으로 등록한 첫 WAL 세그먼트와 결속해 재검증했다. 임의 timeout이나
일반 복원 실패를 WAL 누락으로 판정하지 않는다.

이것은 **주입한 결함이 실제로 거부됨**을 확인한 결과다. DR-01 PASS·정상 복원·팀 AWS/RKE2·
PITR·중간 WAL 손상·복구 시간 보장을 의미하지 않는다. 실제 로그·백업 메타데이터·
리소스 UID·생성 영수증은 비공개로 보존한다.
