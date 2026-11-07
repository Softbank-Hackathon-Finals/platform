# [ADR-0037] AUD-01.1은 지금 CNPG primary 자체 로그(pods/log)에서 검색한다

* **상태 (Status):** 채택됨(Accepted). 2026-11-07 T34 담당 결정. 중앙 전달 경로가 생기면 대체한다.
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** T34 #56, T14, T19, `docs/controls/README.md` 4절 AUD-01.1, ADR-0016, ADR-0010

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** `demo-gate` v1의 AUD-01.1은 DB 관리자 로그인이 감사 로그에서 검색되는지 본다. T5 기준(`docs/controls/README.md` 4절)은 다음 경로로 검색한다.
  * 출발지: CNPG 로그를 CloudWatch Logs에서
  * 목적지: 클러스터 로그 수집기(ADR-0016 Object Lock 저장소)에서
* **문제:** 2026-11-07 실제 두 환경에는 이 검색 원천이 없다.
  * 출발지 EKS는 제어면 로그만 CloudWatch로 보낸다. CNPG 컨테이너 로그를 보내는 경로(Fluent Bit · Collector)가 없다.
  * 목적지 Collector(`bravo-observability`)는 지표만 보낸다. 로그 파이프라인과 감사 저장소가 배포되지 않았다.
  * DB 쪽 설정(`log_connections=on`, pgaudit)은 양쪽 모두 켜져 있다.

  그래서 AUD-01.1은 양쪽 모두 UNKNOWN이고, GATE는 그것만으로 BLOCK이다.
* **목표:** 해피패스 GATE에서 AUD-01.1을 실제 원천으로 판정한다. 판정 규칙(`audit_probes._matched`, `AuditSearchProbe`)은 바꾸지 않고, 통과 기준을 낮춘 것을 숨기지 않는다.

## 2. 고려한 옵션들 (Considered Options)

### 1. CNPG primary 자체 로그를 Kubernetes API로 검색 (채택)

로그인과 검색을 다음 순서로 한다.
1. 운영자 kubectl이 primary Pod 안의 로컬 소켓으로 `postgres`(peer 인증) 로그인한다. `application_name=<marker>`를 주고 `rolsuper=true`를 확인한다.
2. 같은 컨테이너의 로그를 `pods/log`(`timestamps`, `sinceTime`)로 읽어 기존 판정기로 검색한다.

양쪽(EKS · RKE2)에 같은 어댑터를 쓴다.

**Pros**
* 지금 바로 두 환경에서 실제 이벤트로 판정할 수 있다. 목적지 실측은 2026-11-07 20:30 KST, 4초에 PASS였다.
* DB 비밀번호를 다루지 않는다.
* 판정 규칙은 그대로다. 같은 사용자 · DB · 표식의 `connection authorized` 줄만 인정하고, 다른 사용자의 같은 표식은 인정하지 않는다.

**Cons**
* **T5 기준(중앙 검색)보다 약하다.** 변조 방지 보관이 아니고, 로그를 쓰는 Pod 자신을 읽는다.
* kubelet이 창 안에서 컨테이너 로그를 회전하면 앞부분을 놓칠 수 있다. 이것은 감지하지 못한다. 놓치면 마감 뒤 `absent`(FAIL)가 되므로, 실패는 막는 쪽으로 난다.
* 운영자에게 DB Pod `pods/exec` · `pods/log` 권한이 필요하다. 플랫폼 서명 원장이 아니라 운영자 실행이다.

### 2. ADR-0016 저장소와 CloudWatch 전달을 배포

**Pros**
* T5 기준 그대로다.

**Cons**
* 출발지와 목적지 각각에 배포할 것이 많다.
  * 출발지: Collector 로그 파이프라인 · 그룹 · IAM
  * 목적지: Object Lock 버킷 · 쓰기/검색 identity · 온프렘 자격
* T37 · 인프라 담당과 맞물려 오늘 해피패스에 맞출 수 없다.

### 3. AUD-01.1을 미룸

**Cons**
* GATE가 계속 BLOCK이다.

## 3. 결정 (Decision Outcome)

옵션 1을 채택한다(T34 담당 결정, 2026-11-07). `operations/controls/run.py`의 `pod-log` 어댑터가 이를 구현하고, 검색은 `bravo_controls.pod_log_audit.PodLogAuditSearch`가 맡는다.

* **불완전하면 UNKNOWN:**
  * 컨테이너가 카나리 전부터 실행 중이 아님
  * 읽기 전후에 Pod UID · 재시작 수 · 시작 시각이 다름
  * 읽기가 바이트 상한에 닿음
  * 표식이 있는 줄의 시각을 읽을 수 없음
* **창:** 3분 동안 3초 간격으로 다시 검색한다. 증거는 마지막 검색 하나만 남긴다.
* **원천 결속:** `kubernetes-pod-log:<환경 ref>:<namespace>/<pod>/<container>:<pod uid>`를 증거에 해시로 묶는다.
* **고지:** 이 경로로 얻은 AUD-01.1 PASS를 "중앙 감사 로그 검색"이라고 말하지 않는다. 데모와 증빙에서는 "DB 인스턴스 자체 로그 검색"으로 적는다.

## 4. 결과 (Consequences)

* T5 문서(`docs/controls/README.md`)는 바꾸지 않는다. 이 ADR이 그 기준에서 벗어나는 범위를 적는다.
* ADR-0016 저장소나 CloudWatch 전달이 배포되면 그 검색기로 어댑터를 바꾸고, 이 ADR을 대체됨으로 표시한다.
* AUD-01.2(DDL) · AUD-01.3(RBAC)는 이 결정에 들어가지 않는다.
