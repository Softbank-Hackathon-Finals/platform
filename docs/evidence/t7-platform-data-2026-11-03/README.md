# T7 플랫폼 DB·접근 역할 실제 검증 — 2026-11-03

이 기록은 기존 AWS EC2/RKE2에 추가한 플랫폼 DB와 출발지 EKS 접근 역할의 검증이다. T10 소비 코드는 #94의 고정 `4d670fb`이며 develop에 `7449745`로 병합됐다. 개인 b-free의 과거 측정이나 목적지 검증을 포함하지 않는다.

| 경로 | 결과 | 증거 |
|---|---|---|
| 로컬 T10 실제 PostgreSQL·공통 계약 | **145 PASS** | 고정 소비 코드, 임시 독립 PostgreSQL; 2개 실패했던 첫 실행은 docs fixture 누락이었고 전체 fixture 포함 뒤 통과 |
| T7 로컬 저장소·역할 경계 회귀 | **11 PASS** | `scripts/tests/test_platform_data.py` |
| 최신 develop 포함 저장소 Python 검사 | **178 PASS · QEMU 검사 1 skip** | 179개 실행, `scripts/tests` 전체 |
| Terraform mock 경계 | **4 PASS** | 암호화·용량·소유자·VPC·사설 호스트 조건 |
| 클라우드 DB 첫 소비 | **26 PASS** | [initial.json](initial.json) |
| DB Pod 재생성 후 별도 Job·새 프로세스 소비 | **26 PASS**, snapshot/receipt SHA 동일 | [restart.json](restart.json), [recovery.json](recovery.json) |
| 상시 정책만 사용한 새 psql 연결 | **8 PASS** | [permanent-path.json](permanent-path.json), 임시 정책 없이 TLS·schema·T10 테이블 조회 |
| Terraform 적용 후 재계획 | **변경 0** | [terraform-drift.json](terraform-drift.json), 전용 data state만 |
| 검증 자원 정리 | 완료 Job 2개·임시 다운로드 정책 제거, bundle 보존 | [cleanup.json](cleanup.json) |
| 출발지 EKS 실제 collector/Executor 단기 토큰 | **18 PASS** | [source-access.json](source-access.json) |
| 플랫폼 세 ServiceAccount API 접근 및 두 역할의 DB 네트워크 | **11 PASS** | [platform-isolation.json](platform-isolation.json) |
| 별도 EBS 읽기 확인 | 암호화·16GiB·종료 시 보존·component 태그 | [storage.json](storage.json) |

첫/재생성 후 26개 소비 검사는 임시 `t7-verification` 정책을 사용하는 동안 실행했다. 상시 `runtime-database` egress를 적용하고 임시 정책을 제거한 뒤, 별도의 새 psql Job에서 TLS·schema·상태/이벤트/영수증 테이블 읽기를 8개 항목으로 확인했다. 앞선 26개 전체 소비 검사를 상시 정책으로 재실행했다고 주장하지 않는다.

DB 검사는 verify-full TLS/SCRAM으로 실제 제한 runtime 로그인에 접속한다. 합성 계획 COLLECT→ANALYZE, 8개 동시 동일 명령, 이벤트 순서·watermark·원래 영수증, ASGI snapshot/SSE 재접속, 인증 없음/다른 tenant 거부, DDL·TEMP·이력 삭제·역할 승격 거부와 plaintext 연결 거부를 포함한다. ASGI는 클러스터 검증 Job 내부에서 실행한다. 외부 로그인 API·전체 상태 머신·실제 컷오버 검증은 아니다.

첫 실행의 `process_reopen_preserves_state`는 **동일 프로세스에서 저장소 객체를 다시 연 검사**다. 그 이름을 프로세스 재시작 근거로 쓰지 않으며 원본 JSON을 보존했다. 후속 실행에서는 `store_reopen_preserves_state`로 바로잡았다. 실제 재기동 근거는 DB Pod UID가 바뀐 `restart.json`과, 서로 다른 Job에서 나온 같은 snapshot/receipt 해시다. [consumer-source.json](consumer-source.json)은 두 번들의 SHA 및 T10 SQL SHA를 따로 기록한다.

출발지 역할 시험은 관리자 impersonation 대신 실제 10분 TokenRequest 토큰을 사용했다. collector의 읽기·쓰기 거부, Executor의 정확한 ConfigMap annotation patch, 두 역할의 Secret/다른 namespace/Pod 생성/exec/토큰 발급 거부를 확인했다. 최초 판정에서 TokenRequest 오류의 소문자 `forbidden`을 놓쳤으며, 대소문자를 정규화한 검증기로 다시 실행한 결과를 게시했다. 기존 PgBouncer `data`와 annotation은 원래대로 보존됐다. DB 쓰기 차단이나 PAUSE/RELOAD는 실행하지 않았다.

플랫폼의 collector/Executor는 각각 독립 namespace·ServiceAccount를 사용한다. 플랫폼 DB Secret·Pod·kube-system Secret 조회가 실제로 거부되고, 두 namespace의 새 검증 Pod에서 정상 동작 중인 DB Service로의 TCP 연결이 timeout으로 차단됐다. runtime 계정도 Kubernetes Secret 조회 권한은 없다.

추가 AWS 자원은 전용 gp3와 attachment 두 개다. 기존 EC2/IAM/VPC/root disk는 Terraform 변경 대상이 아니며 DB는 공개 endpoint가 없다. T7 실제 적용은 리뷰 전 사용자 승인 범위의 개발 검증이다. 팀 리뷰 승인과 필수 CI를 충족하기 전에 PR을 병합하지 않는다.

단일 노드/AZ 장애 중 가용성, off-host 백업·복원, 목적지 역할 실측, 지속 실행용 원격 자격 발급·회전, T12 최종 제어 명령, T33 이식·T24 불변 감사는 남는다. T7 이슈 전체는 닫지 않는다.

[execution-manifest.json](execution-manifest.json)은 AWS가 보관한 제출 스크립트를 다시 읽어 실행 source SHA와 대조한 10개 작업의 기록이다. 실행본과 최종 파일 hash를 모두 기록하며, SSM 시각은 작업 실행/결과 조회 시각이고 Job 성능 측정값이 아니다. cleanup의 후속 terminal-condition 방어는 로컬 회귀 테스트로 확인했으며 이전 클라우드 실행에 소급하지 않는다.

Terraform fmt/validate/mock 4개, Ruff, 공개 텍스트 검사와 추가 스택의 Trivy HIGH/CRITICAL 검사가 통과했다. 로컬 gitleaks 8.30은 빈 allowlist를 거부해 같은 기본 규칙만 담은 외부 임시 설정으로 변경 파일을 검사했다. CI의 8.24.3 전체 이력 검사는 PR 상태를 별도로 따른다.
