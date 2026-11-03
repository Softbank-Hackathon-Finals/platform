# [ADR-0004] Cutover Executor 구현 언어: Python 3.12

* **상태 (Status):** 채택됨(Accepted)
* **날짜 (Date):** 2026-11-03
* **관련 (Related):** T12 #22, T10 #19, T20 #30, T25 #35, 설계 문서 8.6 · FR-9 · NFR-1 · NFR-3

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** 설계 문서 8.6은 Executor 계약(HTTP · JSON 호출, Idempotency-Key, epoch, 결과 DONE / ABORT_PRE_WRITE / SAFE_HOLD)만 고정하고 구현 언어는 담당자가 11/3 개발 착수 전에 정하도록 남겨 두었다.
* **문제:** 언어 조건은 "PostgreSQL · K8s 클라이언트 라이브러리가 성숙한 언어"다. 레포의 기존 서비스(`services/rehearsal-runner`, `services/security-findings`)는 Python 3.12 + uv이고, CI에는 Python · Node · Go 검사가 모두 있다.
* **목표:** 계약을 지키면서 해커톤 일정 안에 컷오버 1회 성공과 ABORT_PRE_WRITE · SAFE_HOLD 분기 시험(T12 완료 기준)까지 가는 언어를 고른다.

## 2. 고려한 옵션들 (Considered Options)

### 1. Python 3.12

FastAPI로 호출 API를 받고, psycopg 3으로 출발지 · 목적지 DB와 PgBouncer 관리 콘솔에, 공식 kubernetes 클라이언트로 클러스터에 접속한다.

**Pros**
* 기존 서비스와 같은 도구(uv, ruff, pytest, FastAPI, jsonschema)라 CI `platform` 검사를 그대로 쓴다
* PostgreSQL(psycopg 3) · K8s(kubernetes) 클라이언트가 8.6 조건을 만족한다
* Executor는 정해진 순서로 원격 호출을 보내고 확인하는 프로그램이라 CPU를 거의 쓰지 않는다. 쓰기 정지 시간은 PAUSE · 펜싱 · barrier 도착 대기에서 정해진다

**Cons**
* 클러스터마다 단일 바이너리로 올리기는 Go보다 불편하다(P1 클러스터별 컷오버 에이전트)
* 타입 검사가 컴파일 단계에서 강제되지 않는다

### 2. Go

pgx와 client-go를 쓴다.

**Pros**
* `context`로 단계별 deadline과 취소를 일관되게 다룬다
* 단일 정적 바이너리라 클러스터별 에이전트 배포가 쉽다

**Cons**
* 레포에 Go 서비스가 아직 없어 프로젝트 구성과 공용 코드를 새로 만든다
* 일정 안에서 Python보다 구현 속도가 느릴 것으로 본다(추정)

## 3. 결정 사항 (Decision Outcome)

* **1번(Python 3.12)을 선택한다.** 위치는 `services/cutover-executor/`, 구성은 기존 서비스와 같은 uv 프로젝트다.
* **P1 클러스터별 컷오버 에이전트는 Executor와 별도 구성요소로 보고, 언어는 그 작업에서 다시 정한다.** Go도 후보로 남긴다.

**이유**
* 기존 서비스 · CI와 같은 도구로 바로 시작할 수 있다
* 8.6의 언어 조건(PostgreSQL · K8s 클라이언트)을 만족한다
* Executor와 상태 머신(T10)은 HTTP · JSON 계약으로 나뉘므로, T10이 다른 언어여도 동작에는 영향이 없다

## 4. 결과

**지킬 것**

* 판정 · 실행 모듈 원칙에 따라 Executor 패키지는 LLM 클라이언트를 의존성에 넣지도, import하지도 않는다.
* 단계별 deadline은 클라이언트 타이머만으로 두지 않는다. 클라이언트에서 시간이 끝나도 서버 쪽 작업은 계속될 수 있으므로 `connect_timeout` · `statement_timeout` · `lock_timeout`을 서버 쪽에도 건다.

**남은 일**

* PgBouncer 관리 콘솔(`PAUSE` · `RELOAD` · `RESUME`)은 simple query protocol만 받는 것으로 알고 있다. psycopg 3(autocommit, `ClientCursor`)으로 실제로 명령이 통하는지 k3d에서 확인한다. 통하지 않으면 관리 콘솔 접속만 다른 방식으로 바꾸고, 이 ADR에 결과를 적는다.
* 공용 JSON Schema(Executor 입출력, 무결성 영수증)를 T10과 같이 쓰는 방법은 T10 담당과 정한다.
