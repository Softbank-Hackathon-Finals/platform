# T34 실제 EKS RBAC 감사 이벤트 검색

11월 5일 **20:50:02–20:50:18 KST**, 등록된 출발지 EKS에서 시험용 RoleBinding을
생성·삭제하고 기존 `CloudWatchAuditSearch`와 `AuditSearchProbe`로 실제 이벤트를 찾았다.
`AUD-01.3`은 PASS다. [원문](control-run.json), [증거](evidence.json),
[범위·정리 요약](summary.json), [실행 패키지 지문](source-manifest.json)을 남긴다.

## 실제 동작과 보호 범위

- 개인 SSO의 같은 임시 자격으로 STS 고유 역할 신원·EKS 토큰을 대조하고,
  등록된 API endpoint·CA를 확인한 뒤 검증하는 TLS로 접속했다.
- 고유 시험 namespace에 **규칙이 없는 Role**, 토큰 자동 마운트가 꺼진 ServiceAccount와
  그 Role을 가리키는 RoleBinding을 만들었다. 업무 권한을 부여하거나 기존 Role을 바꾸지 않았다.
- 실제 Kubernetes SelfSubjectReview의 사용자와 create/delete ACK·UID를 영수증에 결속했다.
- 등록된 CloudWatch 감사 log group·정확한 stream 집합을 대상으로 완전한 페이지 검색을 수행했다.
- 이름 문자열만 찾지 않고 `ResponseComplete`, 실제 actor, namespace·RoleBinding 이름,
  성공 상태 코드·행위·시각을 기존 검사기로 대조했다. 검색 이벤트는 4개, 필요한 행위는 2개다.
- 첫 조회는 아직 미수집(`NOT_RUN`), 15초 뒤 두 번째 조회에서 생성·삭제를 모두 확인했다.
  중간 조회도 비공개로 보존하고 최종 증거와 같은 해시를 결과에 결속했다.
- namespace·Role·SA·RoleBinding 4개를 자기 UID로 삭제하고 부재를 확인했다.
  기존 앱 등록은 전후 일치했고 DB 연결·데이터 변경은 없었다. 전용 SSM 세션도 종료했다.

원본 감사 이벤트·actor·자원·실행 등록·정리 영수증과 운영자 glue는 비공개 보존한다.
공개 출력은 marker·출처·이벤트의 해시와 고정 관찰 사실만 포함한다.
초기 사전검사의 DNS 대소문자 비교 및 SelfSubjectReview 201 처리 오류는 별도 보존했다.
두 경우 모두 canary 생성 전에 중단했으며, 정상 API 응답과 인증 실패를 혼동하지 않았다.

## 남은 범위

이 시험은 **출발지 EKS의 Kubernetes 감사 검색**이며, 앱/DB 최소권한이나 권한 상승 탐지가 아니다.
작업자의 조회 권한은 운영자 자격이고 T14 전용 최소권한 검색 신원 인수는 별도다.
DB 로그인·DDL 감사, 목적지 수집·검색, 장기 보존/변조 방지와 전체 T34 인수는 남는다.

다른 19개 통제는 이번 실행에서 UNKNOWN/NOT_RUN이다. T10 운영 생산자 등록·목적지 결과·
최종 양쪽 게이트를 발급하지 않았으며, 기존 실행과 합쳐 전체 PASS로 표시하지 않는다.
양쪽 검색을 요구하는 T34 체크박스와 T14/T19 전체 완료 체크도 올리지 않는다.
