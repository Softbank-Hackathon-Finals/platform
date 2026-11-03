# T10 계획 조회 · 첫 상태 전이 계약 제안

**로컬 제안, T10 전체 완료 아님.** T21 상태 화면과 후속 상태/API 구현을 위한 작은 참조 모듈이다. 기존 [이벤트 계약](T10-events-v1-proposal.md), [ADR-0008](../adr/0008-plan-state-event-transaction.md), 설계 초안 v0.3 §8.9와 함께 검토한다. 새로운 운영 서비스나 실제 환경 등록을 확정하지 않는다.

## 불변 입력과 신뢰 경계

`PlanSpec`은 `plan_id`, `tenant_id`, `workload_id`, `source_environment_id`, `target_environment_id`, `plan_digest`, `collection_scope_digest`를 묶는다. ID는 기존 SSE의 식별자 형식, digest는 소문자 16진수 64자리다. 서버가 등록 때 해당 참조와 tenant 소유권을 검증한 뒤 내부 `register_plan`을 호출해야 한다. 이 모듈은 환경 연결 · 권한 검사나 digest 원본 조회를 수행하지 않는다. 같은 plan ID의 같은 불변 입력은 재시도 가능하지만 한 필드라도 바꾸면 `plan_conflict`다. 이미 존재하는 독립 이벤트 stream은 `stream_conflict`로 거부하며 임의로 계획에 편입하지 않는다.

`collection_scope_digest`는 호스트 수집기가 정한 필수 수집 항목/버전의 digest다. `CollectionProof`는 그 수집기 또는 서버 증거 저장소에서 얻은 `plan_id`, `plan_digest`, `collection_scope_digest`, `evidence_id`, 엄격한 boolean `complete`다. 문자열 `true`나 숫자 1은 완료가 아니다. 이 값은 T5의 전체 15필드 `context_digest`와 별개의 수집 범위다. T5의 PASS/게이트 및 승인/실행 권한을 뜻하지 않는다. 해시 일치만으로 외부 증거가 진짜임을 검증하지 않으므로 HTTP 입력에서 객체를 만들면 안 된다.

## 원자 저장과 명령 재시도

등록은 상태 `COLLECT`, 버전 1, `state.changed`(from null) 이벤트를 한 트랜잭션으로 만든다. `complete_collection`만 `COLLECT → ANALYZE`를 허용한다. proof의 계획/범위가 같고 완료이며 예상 버전이 현재와 같아야 한다. 상태 버전 증가, 기존 SSE 형식의 이벤트, 명령 영수증을 한 SQLite 쓰기 트랜잭션으로 저장한다. 중간 실패 시 셋 다 롤백한다. 관리되는 계획의 상태 이벤트는 일반 `EventStore.append`로 우회할 수 없다. 다른 이벤트 종류는 신뢰된 기존 생산자가 같은 로그에 기록할 수 있다.

멱등 키는 `(plan_id, command_key)`다. proof 전체, `expected_version`, `run_id`, `occurred_at`이 같은 재시도는 최초 `{plan_id, state, version, event_id}` 영수증을 반환한다. 변경된 입력은 `command_key_conflict`다. 호출자는 최초 명령의 run/시각도 보존해야 한다. 이 내부 명령 키를 아직 구현하지 않은 HTTP `Idempotency-Key`나 T12 실행 키와 혼동하지 않는다. 새 키의 과거 버전은 `version_conflict`, 현재 버전으로 같은 전이를 다시 요구하면 `transition_not_allowed`다. 지원하는 임의 `to_state` API는 없다.

## 조회와 이벤트 연결

`create_plan_app(PlanStore(...), resolve_principal=...)`가 다음 읽기 경로를 함께 제공한다.

- `GET /v1/plans/{id}`: [snapshot JSON Schema](../../services/platform-events/src/bravo_platform_events/schemas/plan-snapshot.schema.json), [합성 예시](../../services/platform-events/examples/plan-snapshot.json).
- `GET /v1/plans/{id}/events`: 기존 SSE 계약과 `Last-Event-ID`/`follow=false`를 그대로 사용한다.

호스트가 인증 검증을 마친 `Principal(subject_id, tenant_id)`를 동기 resolver로 제공해야 한다. 기본은 미인증이다. 같은 tenant의 계획만 읽을 수 있으며 tenant는 저장된 불변 등록값과 비교한다. 인증 없음 · 다른 tenant · 미등록 계획은 두 경로 모두 `403 {"detail":{"code":"forbidden"}}`다. client tenant 헤더는 이 모듈에서 읽지 않는다. 저장소/인증 제공자 오류는 데이터를 반환하지 않는 서버 오류이며, 조용히 허용하지 않는다. 실제 OAuth/JWT 검증과 조직 내 역할별 권한은 호스트 통합 과제다.

snapshot은 `Cache-Control: no-store`로 반환하며, 공개 JSON에서 tenant 메타데이터는 제외한다. 상태/버전/불변 참조/시각과 `last_event_id`를 **하나의 DB 읽기 시점**에서 얻는다. 전이가 그 읽기 뒤에 커밋되면 이벤트는 watermark 다음에 존재한다. 이미 커밋된 다른 이벤트 종류도 watermark에 포함되므로 상태 버전과 이벤트 순번이 같다는 가정을 하면 안 된다. 읽기 중 권한 철회도 반환 전 재검사한다.

T21은 snapshot으로 상태 화면을 초기화하고 그 ID 다음부터 SSE를 적용한다. 마지막으로 처리한 ID를 유지하고 재전송된 이벤트는 ID로 중복 제거한다. **snapshot은 계획 상태만 담는다.** 승인 · 게이트 · 실행 결과 · 감사 이력 전체를 복구하지 않으며 T24의 기존 이력 cursor를 대체하지 않는다. 보존 구멍/삭제된 watermark는 기존 SSE의 `409 resync_required`로 남는다. 현재 참조 모듈은 이력 보존 복구나 cursor 재설정을 제공하지 않는다. 응답 헤더 이후 저장 오류/보존 구멍은 스트림 종료 후 재접속에서 확인해야 한다.

## 검증과 다음 단계

로컬 회귀 시험은 등록 재결속 거부, 실패 주입 롤백, 동시 경쟁 단일 승자, 재시작/명령 재시도, 읽기 중 다른 연결의 commit, snapshot 이후 SSE, 미인증/tenant/철회 경계를 포함한다. 실제 로그인·T21/T24 화면 통합·클라우드 E2E 증거는 아니다.

후속은 (1) T5 서버 정본 게이트와 승인 결속 조회, (2) T12 실행 계약/재기동 원격 효과 대조와 SAFE_HOLD, (3) T6/T7 환경의 실제 등록·권한 검증 및 운영 DB 저장 어댑터다. 승인과 외부 실행 기능은 이 작은 단계에 포함하지 않는다.
