# 백업·복원 상태 조회자 권한 분리

`backup_readback_rbac`는 매 작업 고유 이름의 ServiceAccount·Role·RoleBinding을 렌더한다.
자동 적용하거나 토큰을 발급하지 않는다. 환경 소유자가 tenant·namespace·대상 UID·승인을
확인한 뒤 새 자원으로 생성한다. 기존 앱/Executor/Collector 권한에 합치지 않는다.

권한은 지정 이름의 Cluster, Backup, ObjectStore 세 객체에 대한 `get`뿐이다.
Secret·list/watch·Pod exec·create/patch/delete·토큰 발급 권한은 없다.
ServiceAccount의 토큰 자동 mount도 끈다. 실행기는 API 서버 대상의 짧은 TokenRequest를 쓰고,
토큰 발급 권한 자체를 이 읽기 전용 역할에 주지 않는다.

`restore_readback_rbac`는 복원 후 조회용 별도 신원이다. 지정한 **새 복원 Cluster와 읽기 전용
archive ObjectStore 두 객체의 GET**만 허용한다. 원본 Cluster, Backup, 쓰기용 ObjectStore,
Secret, Pod 조회/exec와 모든 변경 권한은 포함하지 않는다. 복원 실행기의 권한을 재사용하지 않는다.
DB의 SELECT 전용 역할과 인증서를 검증하는 연결 resolver는 별도로 결속한다. Kubernetes GET
성공만으로 DB 데이터 복원이나 SQL 접근 통제가 검증됐다고 판단하지 않는다.

Kubernetes RBAC의 resourceNames는 UID를 고정하지 않는다. 같은 이름으로 교체된 자원은
CNPGBackupReader/CNPGRestoreReader가 등록된 지문·UID·generation·상태를 대조해 거부해야 한다. 이 manifest만으로
다른 RoleBinding에서 받은 추가 권한을 제거할 수도 없다. 고유 SA를 만들고 합산된 실제 권한을
검증해야 한다. namespace 관리자·클러스터 관리자·CNPG 컨트롤러는 별도의 신뢰 경계다.

통합 시 순서:

1. 백업 실행기가 요청 전 원본/저장소 지문과 create 응답 UID를 보존한다.
2. 환경 소유자가 별도 상태 조회자 RBAC를 만들고 API 인증·등록을 결속한다.
3. 해당 주체로 3개 GET 성공과 Secret·다른 자원·쓰기 거부를 실측한다.
4. CNPGBackupReader로 완료 상태를 읽고 증거를 보존한다.
5. 작업 종료 시 토큰 재사용을 막고, 소유 UID 조건으로 임시 바인딩을 해제한다.

[실제 로컬 검증](../../../docs/evidence/t35-readback-rbac-2026-11-03/README.md).
opt-in `tests/integration/verify_backup_readback_rbac.py`는 PR #120의 검증기가 설치된 환경에서
서버가 보존한 `RequestBinding` 파일을 사용한다. 업로드된 사용자 JSON을 승인된 요청으로
취급하는 API가 아니다. 실패 결과나 부분 정리는 운영자가 확인한다.

```sh
PYTHONPATH=src python tests/integration/verify_backup_readback_rbac.py \
  --kubeconfig <승인된_시험환경> --request <기존_요청_영수증> \
  --private-output <새_비공개_결과_경로>
```

실제 운영에서는 환경별 실행기·토큰 수명·RBAC 수명·감사 기록을 연동해야 한다.
이 조회자는 백업 생성·복원 실행이나 DB SQL 조회를 담당하지 않는다.

복원용 실제 권한 검증은 같은 도구에 `--mode restore`와 서버가 보존한
`RestoreRequestBinding`을 전달한다. 2개 GET과 신원 지문 확인, 13개 명시적 HTTP 403,
권한 회수 후 거부·재부여 후 성공을 확인한다.
[2026-11-04 로컬 실측](../../../docs/evidence/t35-restore-rbac-2026-11-04/README.md)은
SQL 검증을 대신하지 않는다. 실제 TLS/SELECT 검증은
[별도 복원 조회 시험](../../../docs/evidence/t35-restore-readback-2026-11-04/README.md)에 기록했다.
