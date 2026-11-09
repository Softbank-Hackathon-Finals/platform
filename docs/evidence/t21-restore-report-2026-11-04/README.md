# T21 실제 부분 복원 결과 표시 — 2026-11-04

실제 로컬 T35 복원 조회 → T14 실행 → T19 판정의 바이트·시각을 변경하지 않고 연결했다.
Chromium과 WebKit에서 출발지 DR-01.1 PASS / 목적지 UNKNOWN / 전체 BLOCK을 확인했다.
19개 다른 출발지 시험도 UNKNOWN으로 남는다. 승인·실행·확인 버튼은 비활성이고 변경 요청은0건이다.
쿠키 회수 후 표와 정보가 제거되는 것도 확인했다. 기존 실제 감사 결과(3PASS/17UNKNOWN)도 두 브라우저에서 재검증했다.

인증과 계획 등록은 로컬 시험 fixture이다. 운영 인증·전체 보안 자격·실제 앱 이전을 검증한 결과가 아니다.
실제 원천 보고서와 화면은 비공개 보존하며 공개 파일에는 결과 및 보고서 SHA만 남긴다.

기존 `testing/live_report.config.js`에 실제 보고서 경로·SHA·원래 plan digest를 전달하고,
`BRAVO_TEST_REPORT_KIND=restore-partial`로 실행한다. 보고서를 PASS로 바꾸거나 미수행 시험을 숨기지 않는다.
