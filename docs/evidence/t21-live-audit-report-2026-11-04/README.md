# T21 실제 감사 보고서 표시 검증

2026-11-04 KST. T34의 독립 로컬 k3s/CNPG 두 환경에서 수집하고 T19가 실제 판정한
전체 프로필 보고서를 내용·판정·평가 시각·원래 계획 digest 변경 없이 콘솔에 연결했다.
원본 보고서 SHA-256을 명시해 로드했으며 시험용 T10 계획·신원·보고서 등록을 사용했다.

Chromium과 WebKit 모두 실제 HTTP/SSE를 통해 20개 행을 표시했다. 감사3개 PASS와
미수행17개 UNKNOWN, 전체 BLOCK이 유지됐고 승인·전환 버튼은 비활성 상태였다.
보고서의 `authorizes_cutover`는 false이며 변경 요청은 0건이다.
브라우저 인증 쿠키를 제거한 뒤 이전에 표시한 통제 결과가 지워지는 것도 확인했다.

재현: console 디렉터리에서 `BRAVO_TEST_GATE_REPORT`, `BRAVO_TEST_GATE_SHA256`,
`BRAVO_TEST_PLAN_DIGEST`를 실행자가 검증한 로컬 입력으로 지정하고
`pnpm exec playwright test --config testing/live_report.config.js`를 실행한다.
시험 서버는 127.0.0.1에만 열며 승인·Executor 엔드포인트를 연결하지 않는다.
이 시험은 기존 보고서 조회이지 증거의 유효기간을 갱신하거나 새 전환 승인을 만드는 작업이 아니다.

**미검증:** 팀 운영 SSO·테넌트 등록, 운영 보고서 서명 원장, 실제 클라우드 컷오버.
로컬 신원·계획 등록 fixture를 운영 인증 구성으로 사용해서는 안 된다.
