# T10 이벤트 계약과 로컬 어댑터

**상태: 로컬 제안.** T21 콘솔·T24 감사 로그가 사용할 SSE 계약을 시험하는 모듈이다. `platform-api`가 정해지면 import해서 흡수할 수 있는 구성이고, 별도 운영 서비스·DB·배포 단위를 확정한 것이 아니다. 팀 설계 8.2·8.9, T10 #19를 따른다. 상세 제안은 [SSE 계약](../../docs/contracts/T10-events-v1-proposal.md).

## 구성

- `schemas/event.schema.json`: JSON Schema 2020-12. 버전·이벤트 종류별 payload·결속 필드.
- `contract.py`: 스키마와 계획/순번 일치 검사, UTF-8 SSE 인코딩.
- `store.py`: SQLite를 이용한 로컬 순번·멱등 append·재접속 재생 참조 구현.
- `api.py`: plan-scoped 읽기 전용 FastAPI 어댑터. 인증·인가 callback이 없으면 모든 요청을 거부한다.
- `examples/events.json`: 5종 이벤트의 합성 fixture. 전체 상태 전이 또는 실제 이전의 증거가 아니다.

## 로컬 검사

```sh
uv sync --locked
uv run ruff check .
uv run pytest -q
```

이벤트 종류는 `state.changed`, `approval.requested`, `approval.decided`, `gate.completed`, `execution.updated`다. 영속 순번은 계획별이며 실행 재시도에서 `run_id`가 바뀌어도 증가한다. 제공자 재전송은 `(plan_id, producer_key)`와 입력 해시로 구별한다. 같은 키·입력은 동일 이벤트를 반환하고 다른 입력은 거부한다.

게이트 요약은 현재 [T5 계약](../../docs/controls/README.md)의 `gate_id`, `context_digest`, `decision`(`PASS`/`BLOCK`)을 보존한다. 컷오버 승인 결속에도 같은 게이트 ID·평가 결속값이 필요하며 `plan_digest`는 T5 서버 context와 같은 형식이다. 이 필드들은 서버 정본의 전체 결과 조회·대조와 소비 시점 권한 검사를 대체하지 않는다. 그런 검사는 향후 승인 처리기가 담당하며, 이 모듈은 이벤트 형식만 검증한다.

호스팅 API는 자신의 인증된 요청과 계획 접근 정책으로 `create_app(store, authorize=...)`에 **동기 함수**를 넘긴다. 반환값이 정확히 `True`일 때만 허용한다. 데이터 존재 여부·cursor 형식을 조회하기 전에 접근을 검사하고 스트림 중에도 재검사한다. append·stream 등록은 HTTP로 노출하지 않는다. 인가된 소유 모듈만 Python API를 호출해야 한다.

`GET /v1/plans/{id}/events`는 기본적으로 계속 읽는다. `Last-Event-ID: plan-demo:3`이면 3 다음부터 재생한다. `?follow=false`는 현재 꼬리를 따라잡으면 닫는 시험·재생 옵션이다. 재접속 손실/중복 시험은 실제 ASGI HTTP 경로를 사용하며, live tail·인가 철회는 비동기 스트림을 직접 검사한다.

응답 헤더가 나간 뒤 보존 구멍이나 SQLite 읽기 오류가 생기면 HTTP 상태를 409로 바꿀 수 없어 스트림이 예외로 종료된다. 소비자는 마지막 처리 ID를 유지해 재접속하고, 새 요청의 사전 검사에서 cursor/보존 구멍 오류를 확인해야 한다. 지속되는 SQLite 장애는 현재 서버 오류로 남으며 자동 복구하지 않는다. 연결 종료를 정상 완료로 처리하면 안 된다.

## 구현 범위

스키마 검사는 역사적 승인 기록의 형식을 검증한다. 승인 만료·현재 계획/diff/digest와의 대조, 승인자의 권한, 게이트 통과 여부, 상태 전이 허용 여부는 여기서 결정하지 않는다. 그 판정을 담당하는 코드가 실행 전에 다시 검증해야 한다. SSE를 받았다는 이유로 쓰기나 실행을 열어서는 안 된다.

SQLite 파일은 로컬 참조 저장소이며 S3 Object Lock이나 불변 감사 저장소가 아니다. 운영 상태 DB와 이벤트의 원자적 저장(outbox), 실제 로그인·테넌트 정책, 보존 정책·스냅샷 복구 API, PostgreSQL·다중 인스턴스, 프록시 부하·중단 시험, T21/T24 연결, 팀 리뷰는 남아 있다. CI·클라우드·팀 E2E를 수행했다는 뜻도 아니다.
