# T10 계획 snapshot · 원자 전이 로컬 검증

2026-11-03, Python 3.12.12, SQLite WAL, `uv sync --locked` 환경에서 확인했다. 모든 ID/tenant/digest는 합성 fixture이며 클라우드 리소스나 실제 계정 정보가 아니다.

## 이번 변경

기존 SSE 모듈에 계획 상태 읽기 모델을 추가했다. 내부 등록은 최초 상태/이벤트를 함께 저장하고, 신뢰된 수집 완료 증거에 한해서 COLLECT→ANALYZE를 허용한다. 전이 상태 · 이벤트 · 멱등 명령 영수증은 같은 DB 트랜잭션에 있다. `GET /v1/plans/{id}`는 같은 읽기 시점의 상태/버전과 마지막 이벤트 ID를 반환하고 기존 SSE와 서버 principal/tenant 정책을 공유한다. 기본 접근은 거부한다.

설계 초안 v0.3, [ADR-0007](../../adr/0007-plan-state-event-transaction.md), [조회 계약](../../contracts/T10-plan-snapshot-v1-proposal.md)은 제안 상태다. v0.2 사본은 변경 전 develop `c7822e5fb1f6b4adea4392914ff4010e5a258dcd`의 설계 문서 바이트를 보존했다. 로컬 구현을 운영 서비스/DB 채택으로 간주하지 않는다.

## 재현과 관찰

`services/platform-events`에서 다음을 실행한다.

```sh
uv sync --locked
uv run ruff check .
uv run pytest -q
uv build
```

결과: **77 tests passed** (기존 SSE 40 + 계획/조회 37), Ruff 통과, wheel/sdist 빌드 성공. 기존 Starlette/httpx TestClient 사용에 대한 deprecation warning 1건이 있다.

- 이벤트 INSERT 또는 명령 영수증 INSERT를 실패시키는 SQLite trigger를 주입했다. 상태 · 이벤트 · 순번 · 영수증이 모두 롤백되고 재시도는 예상한 다음 이벤트 ID를 사용했다.
- 서로 다른 명령 8개가 같은 버전에서 경쟁하면 성공 1개/버전 충돌 7개이고 새 이벤트는 1개였다. 같은 명령 16회 병렬 재시도는 같은 영수증/이벤트 1개였다.
- 계획 행을 읽은 뒤 다른 연결이 전이를 커밋하도록 강제했다. 조회는 이전 상태와 이전 watermark를 함께 반환했고 새 이벤트는 그 ID 다음 재생에 포함됐다.
- 같은 계획에 tenant/환경/digest를 재결속하거나, 완료 아닌 증거/다른 수집 범위/과거 버전/두 번째 전이/일반 append로 상태를 우회하는 시도를 거부했다.
- 실제 ASGI GET snapshot 후 전이를 기록하고 SSE로 이어 읽었을 때 해당 전이가 누락 없이 나왔다. 미인증 · 다른 tenant · 미등록 계획 · 조회 중 권한 철회를 거부했다.
- 저장소를 다시 열어 상태/버전/이벤트 순서/원래 명령 영수증을 보존했다. 공개 JSON Schema와 예시는 실제 snapshot과 일치했다.

## 완료 범위와 남은 조건

이 증거는 로컬 계약/저장/읽기 경계에 한정된다. 실제 수집기·환경 등록 검증, OAuth/JWT 및 역할 정책, T5 게이트 정본/승인 결속, T12 실행·재기동 원격 효과 대조, PostgreSQL·다중 인스턴스, T21/T24 화면/감사 통합은 수행하지 않았다. COLLECT→DONE 전체 상태 머신이 완료된 것은 아니다.

snapshot은 계획 상태만 복원한다. `last_event_id`는 이후 이벤트를 이어 읽는 기준이며 승인/게이트/감사 이력 전체를 복구하지 않는다. 보존 구멍 또는 삭제된 watermark는 기존 `resync_required` 오류로 남는다. 서버 인증 resolver와 증거 생산자는 호스트가 신뢰를 검증해야 한다. 클라이언트의 tenant/완료 주장을 그대로 연결하면 이 계약을 충족하지 않는다.

원격 필수 CI와 팀 리뷰 결과는 해당 PR의 현재 head에서 별도로 확인한다. 이 로컬 결과만으로 배포/통합 성공 또는 팀 승인을 표시하지 않는다.
