# T34 → T14 → T19 실제 로컬 감사 입력 검증

2026-11-04 KST. 독립된 두 로컬 k3s/CNPG 환경에서 관리자 TLS 로그인,
비관리자 DDL, Kubernetes RoleBinding 변경을 실제로 발생시켰다.
CRI·API 감사 로그를 mTLS Collector로 수집하고 AuditSearch → T14 Runner의
20개 전체 프로필 → T19 대조·소비 경로로 연결했다. 합성 감사 이벤트를 넣지 않았다.

## 결과

- 양쪽 실제 감사 시험 AUD-01.1/01.2/01.3만 PASS.
- 실행하지 않은 17개 시험은 UNKNOWN으로 유지. 전체 게이트는 **BLOCK**.
- 이 BLOCK을 전환 선행조건으로 소비하면 `SECURITY_GATE_BLOCKED`.
- 행은 BLOCK인 채 전체 결과만 PASS로 고친 문서는 `SCHEMA_REJECTED`.
- 유효기간 만료·회수된 서명 키는 `SIGNING_AUTHORITY_REVOKED_OR_EXPIRED`.
- 생성한 시험 컨테이너·네트워크 정리 확인. 기존 환경과 클라우드 자원은 변경하지 않음.

`result.json`의 `status: PASS`는 **위 거부 조건을 포함한 시험의 성공**이다.
20개 보안 통제의 통과나 운영 컷오버 허가를 뜻하지 않는다.

## 재현과 신뢰 경계

`services/control-gate/testing/verify_live_audit_gate.py`를 control-gate 개발 환경에서 실행한다.
`--runner-checkout`에는 T34의 `verify_live_audit_canaries.py`가 포함된 명시적 로컬 체크아웃을,
`--private-output`에는 존재하지 않는 비공개 출력 디렉터리를 지정한다.
T34 Runner 연결 구현은 커밋 `37aa277` 기준이다. 이 인자는 사용자 업로드나 URL 입력이 아니다.
로컬 Docker·Helm·kubectl 및 CNPG 시험 이미지가 필요하며 시험용 자원을 생성하고 정리한다.

Ed25519 서명 검증은 실제 코드다. 서명 키·권한 저장소·운영자 신원은 이 시험만의
메모리 fixture이며 운영 KMS/SSO/원장 연결을 검증한 것은 아니다. 개인 키는 저장하지 않는다.
원문 로그와 등록 입력은 비공개 보존하고 이 문서는 결과 및 해시만 공개한다.

**남은 운영 검증:** 팀 EKS/RKE2 수집 경로, 실제 운영 서명·승인 원장,
불변 감사 보관, 누락된 17개 통제의 실환경 실행 및 T10 실행 직전 재검사.
로컬 결과로 이를 대신하거나 금융 보안 인증 완료를 주장하지 않는다.
