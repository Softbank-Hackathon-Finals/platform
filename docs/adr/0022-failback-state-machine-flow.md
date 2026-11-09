# [ADR-0022] 되돌리기 상태 머신 흐름: WATCH에서 운영팀 승인, 전용 binding, ABORT 뒤 WATCH 복귀, Edge만 재시도

* **상태 (Status):** 제안됨(Proposed). 결정은 T20 spec과 [#222](https://github.com/Softbank-Hackathon-Finals/platform/pull/222)에서 확정했고, 이 문서는 병합 뒤 남기는 기록이다. 시험용 Edge · 증거 reader로 검증했다. 실제 Edge(T17)와 실제 환경 되돌리기(T25) 확인 뒤 채택한다.
* **날짜 (Date):** 2026-11-05
* **관련 (Related):** T20 #30, T10 #19, T17, T21 #27, T24, T25 #35, 설계 문서 6장 S4 · 8.2 · 8.7 · 8.9, [T10 FAILBACK 계약](../contracts/T10-failback-v1-proposal.md), [ADR-0016](0016-failback-replication-path-check.md)

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** Executor는 반대 방향 명령으로 DB만 되돌린다([#212](https://github.com/Softbank-Hackathon-Finals/platform/pull/212), [ADR-0016](0016-failback-replication-path-check.md)). S4 인수 조건은 그 밖에 운영팀 승인, 트래픽의 출발지 복귀, 복구 시간 기록을 요구한다. 이 일은 상태 머신(T10, `services/platform-events`)이 해야 한다. T10에는 컷오버 승인 · 1회 접수 · GET 관측 · SAFE_HOLD 보존(`cutover.py`)과 재시작 복구(`workflow.py`)가 이미 있다.
* **문제:**
  * 승인 조건을 정해야 한다. 감시 FAIL을 요구하면 실수 승인은 막지만, 감시가 놓친 장애에서는 되돌릴 수 없다.
  * 컷오버 승인 binding은 `diff_hash` · `image_digest`를 요구한다. 이 값은 되돌리기와 관계가 없다.
  * Executor가 ABORT_PRE_WRITE로 끝나면 계획을 어느 상태에 둘지 정해야 한다. WATCH 재개는 SHIFT 버전을 "현재 버전 − 2"로 계산한다. ABORT로 버전이 올라간 뒤 WATCH로 돌아오면 이 계산이 틀린다.
  * DB 복귀는 끝났는데 Edge 복귀만 실패할 수 있다. 이때 DB 명령을 다시 보내면 안 된다.
  * `state.changed`는 `from` · `to`만 담는다. 승인자 · 복구 시간 · 영수증 ID를 콘솔(T21)과 감사(T24)에 전달할 곳이 없다.
* **목표:** Executor POST는 최대 한 번이다. 상태를 확인할 수 없으면 SAFE_HOLD로 둔다. 무결성 일치가 확인되기 전에는 Edge를 부르지 않는다. 기존 컷오버 · 변경 승인 계약은 바꾸지 않는다.

## 2. 고려한 옵션들 (Considered Options)

결정할 것이 여섯 가지다. 각 항목에서 앞의 옵션을 선택했다.

### 1. [코드 구조] 별도 `FailbackService` + 결과 반영 공통화

`failback.py`에 별도 서비스를 둔다. 컷오버의 결과 반영(`_project`)만 `project_observation`으로 옮겨 키 접두사 · 진행 상태 · 확정 DONE 처리를 인자로 받게 한다.

**Pros**
* 변경 승인(`change.py`)과 같은 구조다. 검증된 컷오버 분기에 조건이 섞이지 않는다.
* 기존 `test_cutover.py` · `test_workflow_recovery.py`가 수정 없이 통과한다.

**Cons**
* 승인 · 접수 코드 일부가 컷오버와 겹친다.

대안은 `CutoverService`에 종류 인자를 더하는 방식이었다. 중복은 줄지만 컷오버 분기 곳곳에 되돌리기 조건이 생긴다.

### 2. [승인 조건] WATCH + 운영팀 결정

계획이 WATCH이고 운영팀이 결정하면 승인한다. 최근 감시 판정은 참고 기록으로만 둔다.

**Pros**
* S4의 "알림 → 운영팀이 결정 · 승인"과 같다.
* 감시가 놓친 장애나 감시 자체가 고장 난 경우에도 되돌릴 수 있다.

**Cons**
* 감시 FAIL 없이도 승인할 수 있어, 실수로 승인하는 것을 코드가 막지 않는다. 대신 binding 검증과 별도 권한(`approve_failback`)으로 범위를 좁힌다.

대안은 감시 FAIL · 재시험 FAIL 증거를 필수로 받는 방식이었다. 실수 승인은 막지만, 되돌려야 할 때 못 되돌릴 수 있고 증거 연결을 새로 만들어야 한다.

### 3. [승인 binding] 되돌리기 전용 binding

계획 · 반대 방향 · 직전 컷오버 실행 · epoch · 영수증 · 만료만 담는 binding을 새로 둔다.

**Pros**
* 기존 공용 binding과 컷오버 · 변경 승인 계약을 바꾸지 않는다.
* 되돌릴 컷오버(확정 DONE 실행과 그 실행에 결속된 일치 영수증)를 승인에 직접 묶는다.

**Cons**
* 승인 형식이 하나 늘어 콘솔(T21)이 따로 표시해야 한다.

대안은 공용 binding에 칸을 추가하는 방식이었다. 공용 정의가 바뀌고, 컷오버 승인과 섞이지 않게 막는 규칙이 따로 필요하며, 관계없는 `diff_hash` · `image_digest`를 요구하게 된다.

### 4. [원격 ABORT 뒤 상태] WATCH 복귀 + 저장 기록으로 조회

원격 ABORT_PRE_WRITE를 관측하면 `failback.recorded(aborted)`를 남기고 WATCH로 돌아간다. WATCH 재개는 컷오버 명령과 Integrity 체크포인트를 버전 계산이 아니라 저장 기록에서 찾는다.

**Pros**
* ABORT_PRE_WRITE 뒤에는 온프렘이 그대로 주본이고 Edge도 바뀌지 않았다. WATCH가 실제 상태와 맞다.
* 확인된 실패를 사람 대기로 만들지 않는다.

**Cons**
* 다른 담당 코드(`workflow.py`)의 WATCH 재개 조회 방식을 바꿔야 한다.

대안은 세 가지였다. FAILBACK 유지 + 중단 기록은 조회 수정이 필요 없고 현재 컷오버 동작(ABORT 뒤 CUTOVER에 머묾)과 같다. 다만 계획 상태가 실제와 다르다. NEEDS_HUMAN · SAFE_HOLD는 확인된 실패를 사람 대기로 만든다.

### 5. [Edge만 실패] FAILBACK 유지 + Edge만 재시도

DB 복귀와 무결성 일치가 끝난 뒤 Edge 복귀가 실패하거나 확인되지 않으면 FAILBACK에 머문다. `failback.recorded(edge_pending)`를 남기고, Edge 경로(`…/edge`)로 Edge만 다시 부른다.

**Pros**
* 되돌리기가 덜 끝난 실제 상태와 맞다. 상태 enum과 콘솔 변경이 가장 작다.
* DB 명령을 다시 보내지 않는다. Executor 요청 없이 Edge만 재시도한다.

**Cons**
* 콘솔에서 "Edge 복구 대기"가 상태가 아니라 기록으로만 보인다.

대안은 새 상태를 두거나 NEEDS_HUMAN으로 보내는 방식이었다. 새 상태는 계약 변경이 커진다. NEEDS_HUMAN은 뜻이 다르고 재시도 경로가 없다.

### 6. [기록] 새 이벤트 `failback.recorded`, 복구 시간은 명령 접수부터

`failback.recorded`(`edge_pending` · `rolled_back` · `aborted`)에 승인자 · 시작/종료 · 복구 시간 · 영수증 ID를 담아 SSE로 보낸다. 복구 시간은 명령 접수 시각(`dispatch_claimed_at`)부터 Edge 복귀 확인까지다.

**Pros**
* 콘솔과 감사가 따로 조회하지 않고 이벤트로 받는다.
* 복구 시간에 사람의 결정 시간이 섞이지 않는다. 승인 시각은 승인 기록에 따로 남는다.

**Cons**
* 이벤트 종류가 하나 늘어 T21 · T24가 새 형식을 받아야 한다.

대안은 `plan_commands`에만 두고 되돌리기 조회 API 필드로 공개하는 방식이었다. 스키마 변경은 작지만 콘솔 · 감사가 따로 조회해야 한다. 복구 시간을 승인 시각부터 재는 방식은 사람의 결정 시간이 섞여 시스템 시간을 보여 주지 못한다.

## 3. 결정 사항 (Decision Outcome)

* **옵션 1~6을 모두 선택한다.** 흐름은 [T10 FAILBACK 계약](../contracts/T10-failback-v1-proposal.md)의 순서를 따른다.
  * 승인은 WATCH에서만 받는다. binding은 저장 기록에서 찾은 확정 DONE 컷오버 실행 · epoch · 일치 영수증과 같아야 한다. 권한은 `approve_failback` · `execute_failback`로 컷오버 권한과 나눈다.
  * 명령은 등록 DB 쌍의 정확한 반대 방향이다. epoch는 이 계획이 쓴 모든 epoch(컷오버와 이전 되돌리기 포함)보다 커야 한다. 접수 · 승인 소비 · `WATCH → FAILBACK`을 한 트랜잭션에 저장한 뒤에만 POST를 한 번 보낸다. 이후 재시도 · 응답 유실 · 재시작에는 GET만 한다. 멱등 키 접두사는 `failback-`이다.
  * 확정 DONE 뒤에는 이번 실행에 결속된 일치 증거만 인정한다. 증거가 없거나 조회가 실패하면 FAILBACK에서 기다린다. 다른 실행의 증거나 불일치는 SAFE_HOLD다. 두 경우 모두 Edge를 부르지 않는다.
  * Edge 호출 권한은 잠금 안에서 먼저 저장한다. 실제 호출은 잠금 밖에서 한다. 완료 기록 전에 상태를 다시 확인하고, FAILBACK일 때만 `DONE_ROLLED_BACK`을 한 번 기록한다.
  * 상태를 바꾸는 것은 현재 버전의 진행 명령뿐이다. ABORT로 끝난 이전 실행의 늦은 조회나 Edge 재시도는 관측만 기록한다.
* **원격 ABORT 뒤 승인은 소비된 채 남는다.** 재시도하려면 새 승인과 새 epoch가 필요하다.
* **컷오버 확정 뒤 SAFE_HOLD에서의 되돌리기는 이 흐름에 넣지 않는다.** 설계 8.2대로 사람이 판단한다.

**이유**
* 실행 횟수와 상태 판정을 코드가 쥔다. 사람은 승인만 하고, 승인 값은 실제 되돌릴 컷오버 기록에 결속된다.
* 상태를 확인할 수 없으면 SAFE_HOLD로 멈추고, 무결성이 확인되기 전에는 트래픽을 옮기지 않는다. 쓰기를 받는 DB는 계속 최대 하나다.
* 기존 컷오버 · 변경 승인 경로를 바꾸지 않고 같은 구조를 재사용해 회귀 위험이 작다.

## 4. 결과

* `services/platform-events` 시험 734개가 SQLite와 임시 PostgreSQL에서 통과했다(#222 시점). 확인한 상황은 1회 POST, 응답 유실, 접수 직후 종료(POST 0), 동시 요청, 저장 실패, 결속 불일치, 반대 방향 · epoch, 증거 3종, Edge 실패 · 재시도 · 성공 직후 종료, 보류 경합, ABORT 두 번 뒤 `WATCH → DONE`, 권한 분리와 취소다.
* 일부러 틀린 구현을 넣어 새 시험이 실패하는지 확인했다. 대상은 epoch 하한, 방향, DONE 뒤 보류, 완료 전 재확인, 증거 결속, WATCH 재개 조회, 진행 명령 결속, 실행 권한이다.
* 격리 k3s에서 목적지에 쓴 메시지 5개를 복귀 뒤 출발지 DB에서 모두 읽었다. DB 복귀 명령은 1회였다. Edge에는 시험 대역을 썼다([결정 기록 목차의 재현 사례](README.md#재현한-사례를-추가하거나-정정할-때)).
* T21 콘솔은 [#224](https://github.com/Softbank-Hackathon-Finals/platform/pull/224)에서 `approval_kind=failback` · `DONE_ROLLED_BACK` · `failback.recorded`를 받도록 반영됐다.
* 남은 연결:
  * 실제 Cloudflare 가중치 호출(T17)
  * 호스트가 authority와 무결성 증거를 정본에서 읽는 운영 연결(T10 호스트)
  * 실제 두 환경의 되돌리기와 복구 시간 측정(T25)
