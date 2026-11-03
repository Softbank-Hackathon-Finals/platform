# T7 플랫폼 DB·접근 역할 실제 검증 — 2026-11-03

이 기록은 기존 AWS EC2/RKE2에 추가한 플랫폼 DB와 출발지 EKS 접근 역할의 검증이다. T10 소비 코드는 #94의 고정 `4d670fb`이며 develop에 `7449745`로 병합됐다. 개인 b-free의 과거 측정이나 목적지 검증을 포함하지 않는다.

| 경로 | 결과 | 증거 |
|---|---|---|
| 로컬 T10 실제 PostgreSQL·공통 계약 | **145 PASS** | 고정 소비 코드, 임시 독립 PostgreSQL; 2개 실패했던 첫 실행은 docs fixture 누락이었고 전체 fixture 포함 뒤 통과 |
| T7 로컬 저장소·역할 경계 회귀 | **11 PASS** | `scripts/tests/test_platform_data.py` |
| 최신 develop 포함 저장소 Python 검사 | **178 PASS · QEMU 검사 1 skip** | 179개 실행, `scripts/tests` 전체 |
| Terraform mock 경계 | **4 PASS** | 암호화·용량·소유자·VPC·사설 호스트 조건 |
| 클라우드 DB 첫 소비 (기존 기록) | **25개 유효, 평문 거부 1개 UNKNOWN으로 정정** | [initial.json](initial.json) |
| DB Pod 재생성 후 별도 Job·새 프로세스 소비 (기존 기록) | **25개 유효, 평문 거부 1개 UNKNOWN으로 정정**, snapshot/receipt SHA 동일 | [restart.json](restart.json), [recovery.json](recovery.json) |
| 상시 정책만 사용한 새 psql 연결 | **8 PASS** | [permanent-path.json](permanent-path.json), 임시 정책 없이 TLS·schema·T10 테이블 조회 |
| Terraform 적용 후 재계획 | **변경 0** | [terraform-drift.json](terraform-drift.json), 전용 data state만 |
| 검증 자원 정리 | 완료 Job 2개·임시 다운로드 정책 제거, bundle 보존 | [cleanup.json](cleanup.json) |
| 출발지 EKS 실제 collector/Executor 단기 토큰 (기존 기록) | **16개 유효, exec 거부 2개 UNKNOWN으로 정정** | [source-access.json](source-access.json) |
| 플랫폼 세 ServiceAccount API 접근 및 두 역할의 DB 네트워크 (기존 기록) | **API 9개 유효, 네트워크 2개 UNKNOWN으로 정정** | [platform-isolation.json](platform-isolation.json) |
| 별도 EBS 읽기 확인 | 암호화·16GiB·종료 시 보존·component 태그 | [storage.json](storage.json) |

[P1 판정 정정](p1-correction.json): 이전 검증기는 TCP timeout과 일반 DB 연결 오류를 각각 네트워크/평문 거부로 집계했다. 대상 중단·경로 장애·인증 실패를 배제하지 못했으므로 해당 보안 판정은 UNKNOWN이다. 원본 JSON·해시는 보존하며, 아래 신규 실행 결과를 과거 실행이나 DB 재생성 시험에 소급하지 않는다.

### P1 수정 후 별도 실측

- [플랫폼 API·네트워크 재시험](p1-network-controls-v1.json): **11 PASS**. 두 시험 주체 각각 DB 전후 허용 접속, 같은 Pod의 DNS·대조 서비스 접속을 확인했다. DB 접속만 timeout이었고 각 시험 창은 약 11초, 정책·대상·Pod는 전후 같았다. 고유 시험 Pod에만 DNS/8080을 허용하는 임시 대조 정책을 사용했으며 기존 DB 허용 정책은 변경하지 않았다.
- [새 T10 Job](p1-tls-read-v2.json): **26 PASS**. 평문 검사는 같은 서버 주소/기동 세대에 `connected_tls → rejected_no_encryption → connected_tls`를 43ms 안에 확인했다. 고정 T10 소비 코드와 기존 스키마를 사용한 새 합성 ID의 소비 시험이다. DB Pod 재생성이나 출발지·목적지 컷오버를 다시 실행한 결과는 아니다.
- [정리](p1-probe-cleanup-v1.json): 완료된 새 Job 1개와 임시 다운로드 정책을 제거하고 immutable bundle과 DB 자원을 보존했다. 네트워크 시험 자원은 검증기 자체의 UID 확인 후 정리 경로가 성공했다.
- [실행 결속](p1-execution-manifest.json): AWS가 보관한 명령의 실제 Python source·JSON 입력 hash를 로컬 receipt와 대조했고, 제출된 immutable bundle의 `verify_t10.py`/`tls_verification.py`가 최종 파일과 동일함을 확인했다. 검증기/대조 실패 회귀 15건과 기존 경계 11건이 로컬에서 통과했다.

첫/재생성 후 26개 소비 검사는 임시 `t7-verification` 정책을 사용하는 동안 실행했다. 상시 `runtime-database` egress를 적용하고 임시 정책을 제거한 뒤, 별도의 새 psql Job에서 TLS·schema·상태/이벤트/영수증 테이블 읽기를 8개 항목으로 확인했다. 앞선 26개 전체 소비 검사를 상시 정책으로 재실행했다고 주장하지 않는다.

DB 검사는 verify-full TLS/SCRAM으로 실제 제한 runtime 로그인에 접속한다. 합성 계획 COLLECT→ANALYZE, 8개 동시 동일 명령, 이벤트 순서·watermark·원래 영수증, ASGI snapshot/SSE 재접속, 인증 없음/다른 tenant 거부, DDL·TEMP·이력 삭제·역할 승격 거부와 plaintext 연결 거부를 포함한다. ASGI는 클러스터 검증 Job 내부에서 실행한다. 외부 로그인 API·전체 상태 머신·실제 컷오버 검증은 아니다.

첫 실행의 `process_reopen_preserves_state`는 **동일 프로세스에서 저장소 객체를 다시 연 검사**다. 그 이름을 프로세스 재시작 근거로 쓰지 않으며 원본 JSON을 보존했다. 후속 실행에서는 `store_reopen_preserves_state`로 바로잡았다. 실제 재기동 근거는 DB Pod UID가 바뀐 `restart.json`과, 서로 다른 Job에서 나온 같은 snapshot/receipt 해시다. [consumer-source.json](consumer-source.json)은 두 번들의 SHA 및 T10 SQL SHA를 따로 기록한다.

[exec 권한 질의 정정](exec-authorization-correction.json): 기존 `auth can-i create pods/exec`는 `exec`라는 이름의 Pod를 질의할 수 있어 실제 exec 하위자원 거부 근거가 아니었다. 원본의 두 `*_denied_exec` 판정은 UNKNOWN이며 JSON 바이트를 보존했다. 수정 검증기는 현재 Pod 이름마다 `--subresource=exec`로 GET/CREATE를 각각 질의한다. 이후 #114의 opt-in Role이 허용한 실행 권한은 baseline 실패로 드러나야 하며, 이를 없애려고 정상 cutover 권한을 변경하지 않는다. 당시 기본 역할 시험과 현재 opt-in 역할 시험을 합치지 않는다.

[수정 질의의 현재 출발지 실측](exec-authorization-readback.json)은 기존 단기 토큰과 SelfSubjectAccessReview만 사용했다. 조회한 Pod 3개는 전후 같았으며 collector GET/CREATE 6개는 모두 DENY, Executor는 의도된 앱/풀러 4개 ALLOW와 DB Pod 2개 DENY였다. 따라서 baseline은 **예상대로 FAIL**이다. 이는 현재 opt-in 권한을 baseline 거부 성공으로 오인하지 않는다는 확인이며, 실제 exec 세션·ConfigMap patch·배포 변경은 수행하지 않았다. 원래 기본 역할이 적용됐던 시점의 exec 거부를 소급 입증하지 않는다.

출발지 역할 시험은 관리자 impersonation 대신 실제 10분 TokenRequest 토큰을 사용했다. collector의 읽기·쓰기 거부, Executor의 정확한 ConfigMap annotation patch, 두 역할의 Secret/다른 namespace/Pod 생성/토큰 발급 거부를 확인했다. exec 거부는 위 질의 결함으로 입증되지 않았다. 최초 판정에서 TokenRequest 오류의 소문자 `forbidden`을 놓쳤으며, 대소문자를 정규화한 검증기로 다시 실행한 결과를 게시했다. 기존 PgBouncer `data`와 annotation은 원래대로 보존됐다. DB 쓰기 차단이나 PAUSE/RELOAD는 실행하지 않았다.

플랫폼의 collector/Executor는 각각 독립 namespace·ServiceAccount를 사용한다. 플랫폼 DB Secret·Pod·kube-system Secret 조회가 실제로 거부되고, 두 namespace의 새 검증 Pod에서 DB Service 접속 timeout이 관측됐다. 이전 실행에는 같은 시험 창의 대조군이 없어 이 관측만으로 정책 차단을 입증하지 못한다. runtime 계정도 Kubernetes Secret 조회 권한은 없다.

추가 AWS 자원은 전용 gp3와 attachment 두 개다. 기존 EC2/IAM/VPC/root disk는 Terraform 변경 대상이 아니며 DB는 공개 endpoint가 없다. T7 실제 적용은 리뷰 전 사용자 승인 범위의 개발 검증이다. 팀 리뷰 승인과 필수 CI를 충족하기 전에 PR을 병합하지 않는다.

단일 노드/AZ 장애 중 가용성, off-host 백업·복원, 목적지 역할 실측, 지속 실행용 원격 자격 발급·회전, T12 최종 제어 명령, T33 이식·T24 불변 감사는 남는다. T7 이슈 전체는 닫지 않는다.

[execution-manifest.json](execution-manifest.json)은 AWS가 보관한 제출 스크립트를 다시 읽어 실행 source SHA와 대조한 10개 작업의 기록이다. 실행본과 최종 파일 hash를 모두 기록하며, SSM 시각은 작업 실행/결과 조회 시각이고 Job 성능 측정값이 아니다. cleanup의 후속 terminal-condition 방어는 로컬 회귀 테스트로 확인했으며 이전 클라우드 실행에 소급하지 않는다.

Terraform fmt/validate/mock 4개, Ruff, 공개 텍스트 검사와 추가 스택의 Trivy HIGH/CRITICAL 검사가 통과했다. 로컬 gitleaks 8.30은 빈 allowlist를 거부해 같은 기본 규칙만 담은 외부 임시 설정으로 변경 파일을 검사했다. CI의 8.24.3 전체 이력 검사는 PR 상태를 별도로 따른다.
