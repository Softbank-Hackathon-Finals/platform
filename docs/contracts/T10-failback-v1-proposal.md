# T10 FAILBACK 승인 · 접수 · 복귀

현재 구현은 **신뢰된 호스트에 붙이는 되돌리기 어댑터**다. 실제 Cloudflare 호출(T17), 무결성 증거 정본 조회, 콘솔 표시(T21), 실제 환경 되돌리기(T25)를 뜻하지 않는다. 설계 6장 S4 · 8.2 · 8.7 · 8.9, [ADR-0016](../adr/0016-failback-replication-path-check.md), [CUTOVER 접수](T10-cutover-submit-v1-proposal.md)를 따른다. 이벤트 형식은 [이벤트 계약](T10-events-v1-proposal.md) 「되돌리기(T20) 승인과 기록」이다.

## 순서

1. 계획이 `WATCH`이고 `FailbackAuthority`가 같은 계획 잠금 안에서 현재 조건을 검증해야 승인할 수 있다. 되돌릴 컷오버는 저장 기록에서 찾는다. 확정 DONE으로 관측된 컷오버 명령 하나와 그 실행에 결속된 일치 Integrity 체크포인트 하나가 있어야 하고, 없으면 `cutover_record_unavailable`이다.
2. `POST /v1/plans/{id}/approvals`는 `approval_kind=failback`, 전용 `binding`, `expected_version`, `Idempotency-Key`를 받는다. binding의 계획 · 반대 방향 · 컷오버 실행 · epoch · 영수증이 현재 값과 다르면 `binding_mismatch`다. 승인 시 `approval.requested`와 `approval.decided`를 같은 승인 ID · binding으로 한 트랜잭션에 기록해 이력과 SSE 재생에서 요청과 결정을 잇는다. 관리되는 계획에서 `approval_kind=failback`의 요청 · 결정과 `failback.recorded`는 이 어댑터만 기록한다. 다른 생산자가 일반 append로 넣으면 `managed_failback_required`로 거부한다. 그래서 SSE에 보이는 되돌리기 요청은 항상 같은 ID의 결정과 짝을 이룬다. 이 승인 종류는 이번 계약에서 처음 생겨 이전에 저장된 요청은 없다. 다른 생산자가 요청을 먼저 띄우는 흐름이 필요해지면 그 요청 ID를 명시해 결정하는 경로를 따로 추가한다.
3. `POST /v1/plans/{id}/failback`은 승인 ID와 새 `Idempotency-Key`만 받는다. 명령은 등록 DB 쌍의 정확한 반대 방향(출발지 = 계획의 목적지)이어야 한다. epoch는 이 계획이 쓴 모든 epoch(컷오버와 이전 되돌리기 포함)보다 커야 한다(`epoch_not_increasing`).
4. 명령 · 승인 소비 · epoch 사용 · `WATCH → FAILBACK` 이벤트를 한 DB 트랜잭션에 저장한 뒤에만 Executor에 POST를 한 번 보낸다. 재시도 · 응답 유실 · 재시작은 GET만 한다. 전송 전에 종료되면 POST는 0번이고, 이후 GET이 UNKNOWN이면 SAFE_HOLD다.
5. 관측 반영은 컷오버와 같다. 확정 DONE을 저장하기 전의 UNKNOWN · 원격 SAFE_HOLD는 SAFE_HOLD로 저장한다. 확정 DONE을 저장한 뒤의 GET 실패는 상태를 바꾸지 않는다.
6. 확정 DONE 뒤 `IntegrityReader`로 이번 실행(명령 ID · epoch · 실행 ID)에 결속된 일치 증거를 읽는다. 누락 · 조회 실패는 FAILBACK에서 기다리고(`evidence_pending`), 다른 실행 · 불일치는 SAFE_HOLD다. 어느 경우든 Edge를 부르지 않는다.
7. 증거가 맞으면 잠금 안에서 Edge 호출 권한과 `failback.recorded(edge_pending)`를 먼저 저장하고, 잠금 밖에서 `EdgePort.restore_origin`을 부른다. 확인되면 잠금 안에서 상태를 다시 보고, FAILBACK일 때만 `DONE_ROLLED_BACK`과 `failback.recorded(rolled_back)`를 한 번 기록한다.
8. 원격 ABORT_PRE_WRITE는 `failback.recorded(aborted)`와 함께 `WATCH`로 돌아간다. 승인은 소비된 채 남고, 재시도에는 새 승인과 새 epoch가 필요하다.

| 경로 | 하는 일 | 원격 효과 |
|---|---|---|
| `POST /v1/plans/{id}/failback` | 접수 후 POST 1회, 관측, 진행 | Executor POST ≤ 1, Edge 호출 가능 |
| `POST /v1/plans/{id}/failback/{command_id}/reconcile` | GET 관측과 진행 | Executor GET, Edge 호출 가능 |
| `POST /v1/plans/{id}/failback/{command_id}/edge` | DB 쪽이 끝난 뒤 증거 · Edge만 재시도 | Executor 요청 없음 |

상태를 바꾸는 것은 현재 버전의 진행 명령(`failback-active`)뿐이다. ABORT로 끝난 이전 실행의 늦은 조회 · Edge 재시도는 관측만 기록하고 지금 실행의 계획 상태를 바꾸지 않는다(`not_failback`). `reconcile` · `edge` 경로와 진행(`advance`)은 읽기가 아니라 실행이므로 Executor 조회 전과 상태를 쓰는 트랜잭션 안에서 다시 호스트의 실행 검사(`check_execute`, 운영자 연결에서는 `execute_failback`)를 통과해야 한다. 재시작 복구도 이 권한을 가진 호스트 주체로 실행한다.

응답의 `failback`은 `running` · `aborted` · `evidence_pending` · `held` · `edge_pending` · `rolled_back` · `not_failback` 중 하나다. 원격 detail · 접속 참조 · 내부 revision은 반환하지 않는다. 호스트가 되돌리기 서비스를 연결하지 않으면 승인은 422, 경로는 503(`failback_unavailable`)이다.

## 호스트가 제공해야 하는 것

- `FailbackAuthority(connection, principal, plan, now, approved_by, approval_id) → CurrentFailback(binding, command, revision)`: 운영팀의 현재 권한과 결정, 반대 방향 명령, 자원 기준 새 epoch를 검증한다. 기본 구현은 항상 거부한다. 계획 안 epoch 비교는 Executor의 자원별 단조 epoch 검사를 대체하지 않는다.
- `IntegrityReader(connection, plan, command_id, epoch, execution_id) → FailbackIntegrity`: 이번 되돌리기 실행의 T13 비교 결과를 정본에서 읽는다. 기본 구현은 증거 없음이다.
- `EdgePort.restore_origin(plan_id, command_id, execution_id, epoch) → bool`: 같은 인자의 재호출은 멱등해야 한다. `True`는 복귀가 확인됐다는 뜻이다. T17 전까지는 시험용 가짜 구현만 있다.
- 운영자 권한은 `approve_failback` · `execute_failback`이고, 컷오버 권한과 분리된다. 승인 취소는 `revoke_approval(..., approval_kind="failback")`이다. 접수 뒤 취소는 원격 작업을 취소했다고 표시하지 않는다.

계획 조회(`GET /v1/plans/{id}`)의 공개 스키마 `plan-snapshot.schema.json`은 되돌리기 흐름의 상태 `WATCH` · `FAILBACK` · `SAFE_HOLD` · `DONE_ROLLED_BACK`을 허용한다. 그 밖의 상태(SHIFT · CUTOVER · DONE 등)는 기존 범위 그대로다.

재시작 복구(`WorkflowRecovery`)는 FAILBACK이면 확정 DONE 전에는 GET만, 뒤에는 증거 · Edge만 진행한다. WATCH 재개는 컷오버 명령과 Integrity 체크포인트를 버전 계산이 아니라 저장 기록으로 찾는다. 되돌리기가 ABORT로 끝나 버전이 올라가도 `WATCH → DONE`이 막히지 않는다.

## 검증과 남은 연결

합성 authority · 증거 reader · Edge와 시험 전용 컷오버 seed는 tests에만 있다(`tests/test_failback.py`, `tests/test_failback_access.py`, `tests/test_observation_projection.py`). SQLite와 임시 PostgreSQL에서 다음을 확인한다: 1회 POST, 응답 유실, 접수 직후 종료, 동시 요청, 저장 실패, 결속 불일치, 반대 방향 · epoch, 증거 3종, Edge 실패 · 재시도 · 성공 직후 종료, 보류 경합, ABORT 두 번 뒤 `WATCH → DONE`, 권한 분리와 취소. 실제 T17 Edge, 증거 정본, T21 표시, 실제 환경 실행은 남아 있다.
