# T16 전용 reader 접근 검증 — 2026-11-06

05:20:18–05:20:29 KST에 운영자가 기존 개인 SSO로 전용 15분 reader를 받아,
private 출발지 API의 Deployment·ServiceAccount·CNPG 원본 LIST를 각각
HTTP 200으로 읽었다. 개수는 3·7·1이다. 동일 reader로 #371에 필요한
두 IAM 역할, 한 버킷, 두 Secret의 메타데이터, 한 KMS 키의 메타데이터를 읽었다.

범위 밖 IAM 역할 조회는 실제 AccessDenied였다. Kubernetes의 Secret 읽기,
Pod 생성·exec, Deployment·CNPG patch, 다른 namespace의 Deployment LIST는
6개 SelfSubjectAccessReview에서 거부됐다. 같은 TLS 경로의 무자격 요청은
HTTP 401이었다. IAM 정책 시뮬레이션 7건의 explicitDeny는 실제 위험 동작을
시도한 결과와 구분한다. 상세한 등급·시각·소스 해시는 [result.json](result.json)에 있다.

기존 공유 collector와 개인 계정 할당, 앱/ESO 역할, 등록·계획·업무 자원은
수정하지 않았다. 이번 전용 reader/IAM policy/EKS access entry/namespace
Role·RoleBinding만 준비했다. 전용 터널 종료와 로컬 포트 부재를 확인했다.
자격값은 파일·argv·로그로 복사하지 않았다. 원본 export·AWS 식별자는 보호된
인계에만 남겼으며 이 공개 요약에 넣지 않았다.

**공식 inventory scan은 0회이며 생산자 본인 인증 인수는 아직 아니다.**
현재 이미지의 source SHA 후보는 `8d82a9241d3894737778a77d58349e1aee54eca1`이다.
기존 등록의 `66e7419f404f4ec4addc71014aabfcdd44f34688`와 다르므로
`registration_matches`는 false다. 이전 등록을 다시 표시하거나 도구가 변경하지
않는다. 등록 담당이 유효한 새 입력을 결속하면 공식 생산자가 자신의 SSO로
인수하고 한 번 수집한다. 현재 설정의 `scan`은 인증 전에 mismatch로 거부한다.

실측은 커밋 전 helper 파일로 실행했다. 이후 같은 바이트를 커밋한
`f62f410161f217bae71c6e804d648e19868a5264`와 연결했다.
접근 범위·생산자 실행·만료 및 회수는
[운영 안내](../../operations/inventory-reader-access.md)를 따른다.

앞선 두 거부 시도와 EKS entry 생성 응답 확인·조정 이력, 정책 시뮬레이션의
초기 시간 초과는 비공개 실행 원장에 보존했다. 이를 PASS로 세지 않는다.
T7 #12는 기존 완료 상태를 유지하며, 이 기록으로 T16 전체 완료를 주장하지 않는다.
