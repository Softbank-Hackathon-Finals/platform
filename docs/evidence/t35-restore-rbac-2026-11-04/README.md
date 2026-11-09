# T35 복원 조회 권한 검증 — 2026-11-04

기존 소유 로컬 kind/CNPG 환경에서 새 복원본 조회용 ServiceAccount·Role·RoleBinding을
생성하고 API 대상의 10분 TokenRequest로 실제 요청했다. 서버 인증서를 검증했고 프록시와
리디렉션을 사용하지 않았다. 토큰은 메모리에서만 사용했다.

- 새 복원 Cluster와 읽기 전용 archive ObjectStore 두 GET 및 등록 UID·지문 대조 성공.
- Secret, Pod, 목록, 다른 namespace, 원본 Cluster, Backup, 쓰기용 ObjectStore 및 변경 요청
  **13개가 실제 HTTP 403 / Status Forbidden**. timeout·401·404를 거부 증거로 쓰지 않았다.
- RoleBinding 회수 후 동일 토큰 조회 거부, 새 바인딩 생성 후 같은 신원·지문 재확인 성공.
- 기존 백업 조회 모드도 재실행해 세 GET 성공과 9개 거부·회수·재부여 회귀 확인.
- 변경 시험은 서버 dry-run만 사용했다. 새 임시 RBAC 자원만 생성 응답 UID 조건으로 정리했다.
  원본·백업·복원본·ObjectStore는 삭제하거나 변경하지 않았다.
- control-runner 전체 486개·subtest 58개 통과, 환경 조건부 4개 제외.

이 결과는 Kubernetes 상태 조회 권한을 검증한다. SQL 읽기·전체 데이터 무결성·원격 환경 적용은
이 권한 시험의 성공으로 대체하지 않는다. 실제 SQL readback은
[복원 조회 증빙](../t35-restore-readback-2026-11-04/README.md)에서 별도로 확인했다.
팀 AWS/RKE2 및 T35 전체 운영 자격은 미완료다.

원시 요청 영수증과 API 응답은 비공개 보존하고, 공개 파일은 결과·소스 해시만 포함한다.
