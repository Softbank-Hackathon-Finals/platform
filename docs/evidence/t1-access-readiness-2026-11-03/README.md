# T1 접근 사전 준비 검증

2026-11-03에 실행한 **읽기 전용 사전 조회**다. 사용자·permission set·계정 할당·EKS access entry 생성, RKE2 자격 발급, 팀원 초대·전송은 수행하지 않았다.

- [readback.json](readback.json): 지정한 서울 리전과 현재 인증의 관측 결과. 계정 경계는 검토된 로컬 foundation 입력과 대조했으며 식별값은 기록하지 않는다.
- [validation.json](validation.json): 기준 커밋, 검사 소스 해시, 종료 코드, 실행 범위와 로컬 단위 시험 9개의 결과.
- [접근 계약·후속 절차](../../access-readiness.md).

## 확인 결과

검토된 대상 계정과 현재 STS 계정이 일치했다. 로그인 주체 종류는 IAM user였고 개인 SSO 세션은 확인되지 않았다. `ap-northeast-2` 조회에서 접근 가능한 Identity Center 인스턴스 0개, EKS 클러스터 0개였다. 다른 리전이나 다른 권한으로 보이는 인스턴스까지 없다는 뜻은 아니다.

사전 조회는 미준비 항목이 있어 **종료 코드 2**, `team_access=NOT_VERIFIED`다. 의도한 사용자/그룹 배정, 각 개인의 SSO 로그인, EKS/RKE2의 허용·거부 RBAC, T12/T15의 DB/PgBouncer 소비자 시험은 남아 있다. 읽기 전용 인벤토리가 실제 접속 시험을 대신하지 않는다.

단위 검사 9개는 잘못된 계정이면 후속 조회 중단, AccessDenied와 빈 결과 구별, 불완전 응답 거부, STS 세션 ARN의 EKS principal 사용 거부, 조회 명령 허용 목록, 민감값 비출력, 인벤토리와 운영 준비 구별을 확인한다. 새 운영 환경에서 SSO/EKS/RKE2가 동작한다는 증거가 아니다.
