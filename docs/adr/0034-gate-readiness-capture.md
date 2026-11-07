# [ADR-0034] GATE 데이터 준비 관찰을 Executor가 만들고, 운영자 capture가 방금 기록한 것만 서명한다

* **상태 (Status):** 제안됨(Proposed). T13 계약과 다른 점(아래 1)은 T13 담당(김태현) 확인이 남아 있다.
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** T19 #29, T13 #23, T12 #22, T10 #19, #612, `docs/contracts/T13-db-fingerprint-v1.md`, ADR-0011, ADR-0016

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** GATE의 데이터 준비 판정(`assess_readiness`)은 T13 관찰(스키마·복제 지연·시퀀스)과 그 binding·snapshot 서명을 요구한다. 관찰의 유효 시간은 30초다. 해피패스 S1·S2의 GATE와 SHIFT·컷오버 직전 재평가마다 새 관찰이 필요하다.
* **문제:**
  * 실제 환경에서 두 DB에 닿는 곳은 Executor뿐인데, T13 관찰을 만드는 생산자가 없었다.
  * T13 계약은 `readback_next`를 "리허설에서 `correct_sequence()`가 돌려준 `dst_set`"으로 정의한다. 논리 복제는 시퀀스를 옮기지 않으므로, 읽기만 하는 관찰은 해피패스에서 늘 `SEQUENCE_REHEARSAL_FAILED`가 된다.
  * 사람이 30초 안에 관찰을 검토하고 서명할 수 없다.

## 2. 결정 (Decision Outcome)

1. **Executor `POST /v1/readiness`(명령 자격)가 관찰을 만든다.**
   * 스키마와 기본 키는 표마다 두 번 읽는다. 복제 지연은 사전 확인과 같은 계산이다.
   * 시퀀스는 `correct_sequence`로 목적지를 **올리기만** 하고, 다시 읽은 값을 `readback_next`로 낸다. 목적지 앱 역할이 로그인할 수 없고 붙은 세션이 없을 때만 올린다.
   * 끝나지 않은 실행이 있으면 관찰 전과 후에 모두 거부한다(409).
   * **T13 계약과 다른 점:** 리허설이 아니라 GATE 사전 확인 경로에서, 컷오버 lease 없이 실제 목적지 시퀀스를 올린다. 근거는 세 가지다. 올리기만 하므로 값이 내려가지 않는다(운영 제어 함수는 원자적이다). 목적지가 닫혀 있어 `nextval` 호출자가 없다. 컷오버 5단계가 같은 함수로 다시 보정한다. 모의 환경의 `raise_sequence`는 원자적이지 않아, 실행이 시작되면 사후 거부로 관찰을 버린다.
2. **DB 참조는 등록 `connection_ref`다.** 관찰의 `database_ref`와 `replication.*_database_ref`는 컷오버 명령이 싣는 값과 같아야 한다. T10은 SHIFT·컷오버 승인에서 두 값을 대조한다(`PLAN_GATE_DATABASE_PAIR_MISMATCH`). 자원 ID(`resource_id`)를 내면 GATE는 통과해도 승인에서 막힌다.
3. **운영자 `operations/gate/run.py capture`가 방금 기록한 readiness 두 digest만 서명한다.**
   * 다른 서명은 지금처럼 `subjects`를 검토하고 `--confirm`으로 한다.
   * capture는 릴레이가 돌려준 binding·snapshot digest와 `subjects`가 같을 때만 서명한다. 그 사이 다른 기록이 끼면 아무것도 서명하지 않는다(HOLD).
   * 승인 경계가 바뀐다. readiness 서명은 사람이 값을 읽고 하는 승인이 아니라, 운영자가 capture를 실행한 사실과 Executor 관찰의 결속이다. 판정은 여전히 GATE가 한다.
4. **GATE 기록(`--evaluate`)은 데이터 판정이 PASS일 때만 한다.** GATE 보고서는 context마다 한 번만 쓸 수 있으므로, 순간적인 복제 지연 하나로 context를 다시 bind하지 않게 한다. 첫 BLOCK을 일부러 남길 때만 `--record-block`을 쓴다.

## 3. 고려한 다른 안

* **읽기만 하는 관찰:** 계약은 지키지만 해피패스 GATE가 늘 BLOCK이다(사용자 결정으로 기각, 2026-11-07).
* **GATE 판정 규칙 완화(시퀀스는 계산만 확인):** 리허설 요구가 약해진다. 기각.
* **사람이 30초 안에 서명:** 실행 불가. 기각.

## 4. 결과 (Consequences)

* 해피패스의 GATE 경로가 명령 하나가 된다: capture(관찰 → 기록 → 서명) → S1·S2에서만 evaluate.
* Executor는 컷오버 밖에서도 목적지 DB에 올리기 전용 쓰기를 한다. 실행이 진행 중이면 거부하고, 목적지가 열려 있으면 올리지 않는다.
* T13 계약 문서의 `readback_next` 정의(리허설에서)는 이 ADR이 받아들여진 뒤 T13 담당이 고친다.
