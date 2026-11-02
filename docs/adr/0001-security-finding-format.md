# [ADR-0001] 보안 탐지 결과의 공통 형식: OCSF 부분집합

* **상태 (Status):** 제안됨(Proposed)
* **날짜 (Date):** 2026-11-03
* **관련 (Related):** T33, [보안 탐지 통합 설계](../security/detection-integration.md), T21 콘솔

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** AWS(Security Hub · GuardDuty, ASFF 형식), Google Cloud(Security Command Center 형식), 온프렘(Falco 이벤트, Kyverno PolicyReport)의 탐지 결과 형식이 모두 다르다. 킥오프는 이것을 통일해 한 화면에 보이라고 요구한다.
* **문제:** 출처마다 심각도 단계, 상태, 분류 체계가 달라 그대로 나란히 놓으면 "같은 기준"이 되지 않는다.
* **목표:** 출처와 무관한 하나의 형식으로 정규화하고, 새 출처(NAVER Cloud 등)를 매핑 표 추가로 붙일 수 있게 한다.

## 2. 고려한 옵션들 (Considered Options)

### 1. OCSF 1.x 부분집합 + 플랫폼 확장 필드

벤더 중립 오픈 스키마인 OCSF의 Detection · Compliance · Vulnerability Finding 클래스에서 필요한 필드만 쓰고, 플랫폼 고유 정보는 `bravo.*` 확장 필드로 둔다.

**Pros**
* 특정 클라우드에 묶이지 않는다. "Beyond Lock-In" 주제와 맞는다
* AWS Security Lake 등 여러 제품이 이미 OCSF로 내보내므로 매핑 사례와 도구가 있다
* 위협 · 설정 위반 · 취약점을 클래스로 나눠 한 형식 안에서 구분할 수 있다
* 매핑하지 못한 원본 필드를 `unmapped`에 둘 자리가 정해져 있다

**Cons**
* 스키마가 커서 부분집합을 고르고 버전을 고정해야 한다
* 팀원에게 생소하다

### 2. AWS ASFF를 공통 형식으로

AWS Security Hub 형식을 기준으로 다른 출처를 맞춘다.

**Pros**
* AWS 쪽은 변환이 거의 없다

**Cons**
* 공통 형식 자체가 AWS에 묶인다. 이전 플랫폼이 출발지 클라우드의 형식에 의존하게 된다
* SCC · Falco · Kyverno 개념이 ASFF에 잘 맞지 않는다

### 3. 플랫폼 자체 형식

필요한 필드만 직접 정의한다.

**Pros**
* 가장 작고 빠르다

**Cons**
* 심각도 · 상태 · 분류 체계를 처음부터 정의해야 하고 외부 도구와 연결되지 않는다
* 선택 근거를 설명하기 어렵다

## 3. 결정 사항 (Decision Outcome)

* **OCSF 1.x 부분집합 + `bravo.*` 확장 필드를 선택한다.**
* **OCSF 버전은 구현 착수 시 하나로 고정하고, 쓰는 필드 목록은 [설계 문서](../security/detection-integration.md) 5절로 관리한다.**
* **ASFF · 자체 형식은 채택하지 않는다.**

**이유**
* 이전 플랫폼의 공통 형식이 출발지 클라우드에 묶이면 주제와 어긋난다
* 위협 · 설정 위반 · 취약점과 T5 통제 시험 결과를 한 형식에 담을 수 있다
* 새 출처를 매핑 표로 추가하는 구조와 맞는다

## 4. 결과

* 정규화기는 출처별 매핑 표(데이터)와 결정적 코드로만 만든다. LLM을 쓰지 않는다.
* 콘솔(T21)과 조회 API는 이 형식만 안다. 출처가 늘어도 화면은 바뀌지 않는다.
* OCSF 필드 이름 · 열거값은 구현할 때 공식 스키마로 다시 검증한다.
