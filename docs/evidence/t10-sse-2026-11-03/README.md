# [T10] SSE 계약 로컬 검증 — 2026-11-03

범위: `services/platform-events` 및 `docs/contracts/T10-events-v1-proposal.md`. 팀 정본 develop `440a7a25d8a0052d18c325961991d66ebe518b1b`에서 분리한 로컬 제안이다. 기존 팀 운영 서비스·게이트·상태 머신·승인 판단을 변경하지 않았다.

| 검사 | 결과 | 확인 범위 |
|---|---|---|
| `uv sync` | 성공 | Python 3.12.12, lock 생성 |
| `uv run ruff check .` | 통과 | 새 모듈·시험의 정적 검사 |
| `uv run ruff format --check .` | 통과 | Python 파일 6개 |
| `uv run pytest -q` | 31 passed | 스키마·승인 결속 형식·멱등/충돌·재시작·24개 동시 쓰기·cursor 경계·HTTP 재연결·인가·live tail |
| `uv build --wheel` | 성공 | wheel 8개 파일, schema 포함 확인 |
| 합성 fixture 저장 후 새 EventStore 객체로 재접속 | 통과 | `plan-demo:3` 다음 순번 4·5만 재생. `examples/replay-after-3.json`에 결과 |
| `git diff --check` | 통과 | 공백 오류 없음 |
| CI와 같은 Gitleaks v8.24.3의 staged 검사 | 통과 | 137 KB 검사, 유출 없음 |

pytest에는 설치된 Starlette가 기존 httpx TestClient 경로를 deprecated로 표시하는 경고 1건이 있다. 검사 실패는 아니지만 향후 의존성 갱신 시 시험 클라이언트 호환성을 확인해야 한다.

로컬 Gitleaks 8.30.1은 저장소의 기존 빈 allowlist 설정을 읽지 못했다. 설정/예외를 바꾸지 않고 CI에서 사용하는 v8.24.3으로 staged 검사를 통과했다.

초기 시험에서 RFC 3339 형식 검증기가 설치되지 않아 timezone 없는 시각이 허용되는 문제를 발견했다. `rfc3339-validator`를 직접 의존성으로 추가하고 잘못된 시각 거부를 재검증했다. 스키마 검증 실패는 저장 트랜잭션을 되돌리고 순번을 소비하지 않는다.

권한 callback 미제공·모호한 반환값·비동기 함수 오주입을 거부한다. plan 조회/cursor 검사 전에 인가하며 허용된 연결에서도 권한이 철회되면 본문 전송을 중단한다. cursor가 다른 계획이면 400, 미래/삭제/누락된 이벤트면 409를 반환한다. 이미 열린 스트림에서 나중에 발견한 저장소 오류는 연결 실패이며 성공으로 표시하지 않는다.

**미검증/미구현:** 실제 네트워크 프록시·브라우저·클라우드·CI, T21/T24 실제 연결, 사람 로그인, 상태 DB와 이벤트 outbox의 원자성, PostgreSQL/다중 인스턴스, 보존·스냅샷 복구, Object Lock 감사 저장, 승인 대조/게이트/실행, 전체 상태 머신 COLLECT→DONE. 본 기록으로 T10 전체 또는 이슈 체크 완료를 선언하지 않는다.

외부 push·PR·Slack·Notion 게시·클라우드 자원 생성은 수행하지 않았다.

부모 검토 후 문서 보완: 응답 헤더 전송 뒤의 보존 구멍/SQLite 읽기 오류는 새 HTTP 409로 변경할 수 없고 스트림을 종료한다. 소비자는 마지막 처리 ID로 재접속해 cursor 오류를 확인해야 하며, 지속되는 저장소 오류의 자동 복구는 없다. 이 후속 변경은 문서만 수정했고 공백 검사를 통과했다. 실행 코드와 위 31개 시험 결과는 그대로다.
