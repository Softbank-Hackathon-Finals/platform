# T10 PostgreSQL 저장 어댑터 — 로컬 검증

상태: 실제 임시 로컬 PostgreSQL 시험. 운영 플랫폼 DB 구축·팀 통합 완료 증거가 아니다.

- PostgreSQL 14.20(Homebrew), Python 3.12.12, psycopg 3.3.7.
- 기존 SQLite/SSE/계획 시험 77개 유지 + 두 backend 공통 계약 50개 + PostgreSQL 전용 18개 = **145 PASS**.
- 동시 등록/전이/재시도/append는 서로 다른 연결과 어댑터를 사용한다. 계획별 순번, 원래 명령 영수증, stale version 거부를 확인했다.
- event 또는 receipt INSERT를 거부해 상태·event·counter·receipt 전체 rollback을 확인했다.
- 조회 중 다른 연결이 commit해도 상태와 watermark가 같은 snapshot이며 이후 SSE에서 전이를 읽는지 확인했다.
- 실제 DB를 immediate shutdown 후 재시작해 WAL 복구 뒤 snapshot·tenant·receipt·다음 순번이 유지되는지 확인했다. 전원/디스크 손실·복제/HA·백업 복원 시험이 아니다.
- migration은 별도 비superuser login으로 수행했다. runtime login은 정본 DDL, tenant/spec/event 변경, event/receipt 삭제, schema version 수정, migration role 승격, 무관한 스키마 읽기를 거부했다.
- fixture는 임시 Unix socket 전용 cluster이며 기존 DB/클라우드 접속을 사용하지 않는다. 합성 login/trust는 private 임시 디렉터리의 시험 설정이며 운영 HBA 예제가 아니다.

재현: `services/platform-events`에서 PostgreSQL 서버 도구를 준비한 뒤 `uv sync --locked`, `uv run ruff check .`, `uv run pytest -q`. PostgreSQL 도구 누락은 skip이 아닌 실패다. 원격 TLS·secret·RKE2/PgBouncer·운영 권한/보존 정책은 아직 검증하지 않았다.

현재 Starlette TestClient의 httpx 경로에 대한 dependency deprecation 경고 1건이 있고 시험 실패는 없다. 결과와 서버 버전은 로컬 관찰이며 CI 결과는 해당 PR의 최종 head check에서 별도로 확인해야 한다.
