# T21 계획과 보고서의 시험 범위 결속 — 2026-11-06

전체 20개 시험을 요구하는 계획에 3개짜리 데모 보고서를 표시할 수 있었던 조회 경계를 수정했다. T10의 불변 `profile_id`·`profile_version`과 보고서 프로필을 서버와 브라우저가 모두 대조한다. 다른 버전·프로필 누락도 표시하지 않는다. T19 판정·T10 실행 권한은 변경하지 않았다.

## 재현과 확인

- 변경 전 실제 로컬 T10 PlanStore·HTTP와 신뢰를 부여한 합성 보고서로 세 경우를 재현했다. full/1, demo-gate/2, 프로필 없는 계획에 demo-gate/1 PASS 보고서를 붙이면 모두 HTTP 200이었다.
- 변경 후 세 경우 모두 HTTP 503이고 행을 반환하지 않는다. 일치하는 프로필의 정상 표시와 T14→T19→T10→T21 통합 시험은 유지했다.
- 브라우저에는 서버의 정상 3행 보고서와 다른 full/1 계획을 일부러 전달했다. Chromium·WebKit 모두 PASS 문구와 행을 표시하지 않았다. 이 시험 중 변경 요청은 0건이었다.
- 실제 네트워크가 있는 로컬 환경에서 Python 118개, JavaScript 152개, Chromium 22개, WebKit 관련 3개 통과. Ruff·구문·자산 검사 통과. 최초 제한 환경에서 loopback bind 3개가 거부돼 같은 코드를 허용된 환경에서 실행했다.

시험의 신원·계획·보고서는 fixture다. 운영 저장소의 발급자 인증 성공, 양쪽 실제 통제 PASS 또는 실행 게이트 우회를 입증한 것이 아니다. 프로필 없는 기존 계획은 일반 조회를 유지하되 통제 보고서 표시만 거부한다. 기존 계획에 보고서의 프로필을 자동으로 채우지 않는다.

## 재현

`services/console`에서 고정 lock의 `uv sync --locked`, `uv run pytest`, `pnpm test`, `pnpm lint`, `pnpm build`를 실행한다. 브라우저는 `pnpm exec playwright test`, WebKit 경계는 `pnpm exec playwright test --browser=webkit --grep 'twenty-test|report permission|revoked browser'`로 확인한다. 로컬 시험 호스트는 운영 인증 예제가 아니다.
