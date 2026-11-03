# [T10] 계획별 SSE 이벤트 계약 v1 제안

상태: **로컬 구현·시험 제안, 팀 채택 전**. 기준: `docs/design.html` 8.2 상태 머신, 8.9 `GET /v1/plans/{id}/events`, FR-7 승인 결속, FR-14 감사 기록. 소비자는 T21 콘솔·T24 감사 로그다. 설계 원본·tasks.md·보드 상태는 바꾸지 않는다.

## 전송과 재접속

```text
GET /v1/plans/plan-demo/events
Last-Event-ID: plan-demo:3

Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-store
X-Accel-Buffering: no

id: plan-demo:4
event: approval.decided
data: {"schema_version":"1.0", ...}

```

SSE 프레임의 `id`와 JSON `event_id`는 같다. 프레임의 `event`와 JSON `event_type`도 같다. JSON은 한 줄로 인코딩하고 프레임은 빈 줄로 끝낸다. heartbeat는 `: keep-alive` 주석이며 `id`가 없어 재접속 위치를 이동시키지 않는다. `retry: 1000`은 클라이언트의 재접속 간격 힌트다. [WHATWG SSE 규격](https://html.spec.whatwg.org/multipage/server-sent-events.html)을 따른다.

계획별 `sequence`를 커밋 순서대로 1씩 증가시킨다. 타임스탬프로 정렬하지 않는다. 실행을 다시 시도해 `run_id`가 바뀌어도 계획의 순번은 이어진다. 한 계획의 cursor를 다른 계획에서 사용할 수 없다. cursor는 인증 자격이나 실행 권한이 아니다.

| 입력/상태 | 응답 | 소비자 행동 |
|---|---|---|
| 헤더 없음 또는 빈 헤더 | 보존된 최초 이벤트부터 재생 | event_id/sequence로 중복 제거 |
| 현재 계획의 존재하는 ID | 해당 ID 다음부터 재생 | 먼저 처리·저장한 순번 뒤를 적용 |
| 다른 계획, 0/음수/선행 0, 형식 오류 | 400 `invalid_cursor` | 잘못된 cursor를 수정하고 새로 연결 |
| 현재 계획이지만 미래/삭제된 ID, 보존 데이터의 구멍 | 409 `resync_required` | cursor를 임의로 건너뛰지 않고 복구 필요 상태로 전환 |
| 접근 불가 | 403 `forbidden` | 중단. 계획 존재·cursor 유효성은 공개하지 않음 |
| 허용된 요청이나 계획 스트림 없음 | 404 `plan_not_found` | 계획 등록/읽기 경로 확인 |

409 뒤 사용할 계획 스냅샷·기준 cursor API는 이 제안에 구현되지 않았다. 따라서 운영에서는 그 복구 경로를 정하기 전 자동 보존 삭제를 켜지 않는다.

HTTP 응답 헤더가 이미 전송된 뒤 보존 데이터의 구멍이나 SQLite 읽기 오류가 발생하면 새 HTTP 409를 보낼 수 없다. 현재 어댑터는 예외로 스트림을 종료한다. 소비자는 성공으로 처리하거나 cursor를 앞으로 옮기지 않고 마지막으로 처리한 ID로 재접속한다. 재접속의 사전 검사에서 cursor/보존 구멍은 409로 확인할 수 있다. SQLite 읽기 장애가 계속되면 정상 재생되지 않으며, 현재는 별도 오류 매핑이나 자동 복구 없이 서버 오류로 남는다. 이 종료를 성공이나 누락 없는 전송으로 보고하지 않는다.

브라우저의 같은 `EventSource` 객체는 재접속 시 마지막 ID를 전달한다. 새 페이지/새 객체에서 cursor가 자동 복원된다고 가정하지 않는다. `EventSource.onerror`는 HTTP 오류 본문을 읽을 수 없으므로, 세부 400/409 처리가 필요한 T21은 인증된 fetch 스트림을 사용하거나 연결을 닫고 별도 HTTP 조회로 원인을 확인한다. 재시도 횟수 상한 뒤에는 운영자에게 복구 필요를 표시한다.

## 공통 envelope

스키마 원본: [event.schema.json](../../services/platform-events/src/bravo_platform_events/schemas/event.schema.json). 구체적인 예시: [events.json](../../services/platform-events/examples/events.json).

| 필드 | 의미 |
|---|---|
| `schema_version` | 현재 `1.0`. 인식하지 못하면 적용을 멈추고 호환성을 확인 |
| `event_id` | `<plan_id>:<sequence>`. 같은 저장 이벤트 재전송 시 동일 |
| `sequence` | 계획별 양의 정수, JavaScript 안전 정수 범위 이내 |
| `plan_id` | URL의 계획 ID와 같음. 접근 정책은 호스팅 API가 확인 |
| `run_id` | 발생한 실행/시도 ID. 계획별 순번을 대체하지 않음 |
| `occurred_at` | 생산자가 제시한 RFC 3339 시각. 순번/인증 근거가 아님 |
| `event_type` | 아래 5종. 알 수 없는 종류는 버전 협의 없이 추가하지 않음 |
| `data` | 이벤트 종류별 엄격한 구조. 임의 키/시크릿·원본 접속 정보 넣지 않음 |

스키마는 버전·형식·필수 필드를 검사하고, `validate_event`가 계획/순번/ID와 승인 결속의 계획 일치도 확인한다. 다른 언어의 소비자도 이 교차 필드 조건을 검사해야 한다. JSON Schema의 정규식만으로 모든 교차 필드 관계가 검증되는 것은 아니다.

## 이벤트별 payload

| 종류 | 필수 내용 | 전달 의미 |
|---|---|---|
| `state.changed` | `from`(최초 상태 표기용 null 허용), `to` | 소유 상태 머신이 기록한 전이. null 사용 시점과 전이 허용 여부는 소유 상태 머신이 판단 |
| `approval.requested` | `approval_id`, `approval_kind`, `binding` | 화면에서 현재 결속 값을 확인할 승인 요청 |
| `approval.decided` | 위 필드 + `decision`, `actor_id` | 승인/거절/만료의 역사적 결정. 실행 허가 토큰이 아님 |
| `gate.completed` | `gate_run_id`, `verdict`, `evidence_ref` | 게이트 소유자의 판정과 안전한 증거 식별자 |
| `execution.updated` | `execution_id`, `step`, `status`, 선택 `elapsed_ms` | Executor의 관측 상태. 성공 이벤트가 DB 쓰기를 열지 않음 |

`binding`은 `plan_id`, `plan_revision`, `diff_hash`, `image_digest`, `target_environment_id`, `expires_at`를 담는다. `plan_revision`, `diff_hash`, `image_digest`는 `sha256:<64자리 소문자 hex>` 형태다. 컷오버 승인은 `gate_run_id`도 필수다. 변경 승인은 게이트 ID를 선택적으로 담을 수 있다.

계획 revision hash의 정규화 입력, diff hash 계산 방식, 실제 gate 실행 ID 생성과 승인자 주체 형식은 관련 T10/T12/T19/T31 담당이 확정해야 한다. 이 로컬 예시는 합성 값을 사용한다. 결속 값이 스키마에 맞아도 현재 계획·diff·digest·게이트 결과가 다르거나 만료됐다면 실제 승인 실행 모듈은 거부해야 한다. 역사적 `approval.decided`의 만료 시각은 과거일 수 있어 이 이벤트 읽기 모듈은 현재 시각과 비교하지 않는다.

## 생산·소비와 원자성

- 생산자는 자신에게 허용된 계획 스트림만 사용한다. HTTP append 경로는 없다. 로컬 `register_stream`은 이벤트 공간만 만들며 환경/워크로드/계획 등록을 구현한 것이 아니다.
- `(plan_id, producer_key)`의 동일 입력 재시도는 같은 이벤트를 돌려준다. 입력의 종류·run_id·시각·payload 중 하나라도 바뀌면 `producer_key_conflict`다. 생산자는 최초 `occurred_at`와 키를 재시도에서도 유지한다.
- SQLite `BEGIN IMMEDIATE` 안에서 중복 확인·순번 할당·이벤트 저장을 묶는다. 검증 실패는 순번을 소비하지 않는다. 이는 로컬 이벤트 저장에 대한 보장이고 상태 머신 DB와의 원자성은 아니다. [SQLite 트랜잭션 문서](https://www.sqlite.org/lang_transaction.html).
- T21은 처리한 이벤트 ID를 기준으로 중복을 무시하고 예상 순번보다 큰 값은 갭으로 처리한다. 상태 전이와 승인 버튼의 활성화는 인증된 현재 계획/API를 기준으로 확인한다.
- T24는 이벤트 ID별 중복을 제거하고 감사 저장소의 쓰기 성공 뒤에만 자신의 cursor를 저장한다. 네트워크 재전송은 적어도 한 번 전달로 다룬다. exactly-once 소비나 Object Lock 저장을 이 모듈이 제공하지 않는다.
- 유효한 authorization callback을 주입하지 않으면 모든 읽기는 403이다. 접근 검사는 데이터 조회 전에 수행하며, live 스트림 중 권한이 철회되면 전송을 종료한다. 실제 세션·테넌트·로그인 구현은 호스팅 API가 소유한다.

## 이번 검증과 남은 검증

로컬 검사에는 5종 fixture 스키마, 잘못된 버전·필드·시각·결속 거부, 같은 키 재시도/충돌, 재시작, 24개 동시 쓰기의 연속 순번, 계획 간 cursor 차단, 보존 구멍 감지, HTTP 재연결, 기본 거부, live tail·권한 철회가 포함된다. [로컬 어댑터](../../services/platform-events/README.md)는 운영 배포물이 아니다.

T21/T24 소비자 검토, 상태 DB outbox, 승인 처리기와 원격 효과 결속, 스냅샷 복구, 보존 정책, 실제 인증, PostgreSQL·다중 인스턴스·프록시, 클라우드 통합·CI·E2E는 남아 있다. T10 전체 완료 기준인 목업 COLLECT→DONE 및 조건 미충족 전이 거부를 충족했다고 표시하지 않는다.
