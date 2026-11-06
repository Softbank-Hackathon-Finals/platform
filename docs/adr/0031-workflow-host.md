# [ADR-0031] 워크플로 진행은 T10 워크플로 호스트 하나가 맡는다

* **상태 (Status):** 제안됨(Proposed)
* **날짜 (Date):** 2026-11-06
* **관련 (Related):** T10 #19, T31 #33(반영 뒤 T19 재대조), T38, #503

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** 상태 전이는 `WorkflowRecovery.resume_once`가 맡는다. 단계별 증거는 판독기로 받는다. PROPOSE는 `initial_proposal.evidence_reader`, REHEARSE와 GATE는 `ApplicationEvidence`와 `GateRecheck`가 판독기다.
* **문제:** 저장소의 운영 코드, 배포 설정, `operations/` 어디에도 `WorkflowRecovery`를 만들거나 `resume_once`를 부르는 곳이 없다. 판독기는 시험만 통과한 상태다. 그래서 "APPLIED 뒤 T19 호출"을 포함한 ANALYZE 뒤의 전이를 실제로 진행하는 주체가 없다.
* **목표:** plan 진행을 맡는 프로세스를 하나로 정한다. 단계별 생산자는 판독기로만 연결하고, 연결되지 않은 단계는 전이하지 않는다.

## 2. 고려한 옵션들 (Considered Options)

### 1. 에이전트 SDK에 진행을 맡긴다

**Cons**
* OpenAI Agents SDK와 Claude Agent SDK에는 워크플로 진행 기능이 없다. `RunState`와 세션은 에이전트 실행 **하나**의 저장·재개 기능이다.
* 흐름을 AI가 쥐게 되어 설계 원칙("결정적 워크플로가 흐름을 쥔다")에 어긋난다.

### 2. Temporal 같은 durable execution 엔진을 들인다

Temporal의 OpenAI Agents SDK 통합(2026-03 GA)이 흔한 참조 구조다. 결정적 workflow가 흐름을 쥐고, 모델 호출과 도구 호출은 재시도·타임아웃이 있는 activity로 실행한다.

**Pros**
* 재시도, 이력, 타이머를 엔진이 제공한다.

**Cons**
* Temporal 서버와 저장소를 새로 운영해야 한다.
* 이미 있는 plan 상태 머신, 체크포인트, 단일 쓰기 잠금과 이중 원장이 된다.

### 3. 같은 패턴을 기존 상태 머신 위에 둔다 (채택)

* plan 행과 `workflow:<version>` 체크포인트가 durable 이력이다.
* `resume_once`가 멱등한 한 걸음이다.
* 생산자 기록(AI 제안, T15, T31, T19, Executor)은 activity 결과로 보고, 상태별 판독기가 읽는다.

## 3. 결정 사항 (Decision Outcome)

`bravo_platform_events.workflow_host`를 둔다.

* `StageReaders`: 상태 이름으로 판독기를 고른다. 연결되지 않은 상태는 `workflow_reader_unwired`로 멈추고 전이하지 않는다(fail closed). `STAGES` 밖의 상태에는 판독기를 둘 수 없다.
* `WorkflowHost.sweep`: 진행 중인 plan(`STAGES`와 FAILBACK)마다 상태가 멈출 때까지 `resume_once`를 부른다. 각 걸음은 별도의 durable 전이다. 다른 담당이 진행하는 상태(COLLECT, SHIFT 등)는 건드리지 않는다.
* 생산자가 장애를 내면 로그만 남기고 다른 plan을 계속 진행한다. 멈춤 사유(`hold`)는 바뀔 때만 한 줄 남긴다.
* HTTP 경로는 없다. 클라이언트가 증거를 넣는 길은 없다.
* AI는 PROPOSE 판독기 뒤의 activity로만 존재하며, 상태를 고르지 않는다.

## 4. 결과

* 이 PR은 드라이버와 판독기 표만 둔다. 운영 조립은 단계별 PR로 연결한다.
  1. PROPOSE: #503 Job 결과와 T7 publication
  2. REHEARSE, GATE: `ApplicationHost`, `GateRecheck`, T19
  3. AWAIT_APPROVAL, CUTOVER, WATCH
* 주기적 sweep만 한다. 지연이 문제가 되면 `state.changed`나 생산자 기록을 깨우는 신호를 더한다.
* 단일 replica로 둔다. `ApplicationHost`의 파일 잠금이 같은 프로세스 안에 있어야 한다.
