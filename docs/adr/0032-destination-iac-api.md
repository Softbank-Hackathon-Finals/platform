# [ADR-0032] 목적지 IaC를 모듈·원하는 상태(Git)·plan API로 제공하고 에이전트는 계획만 한다

* **상태 (Status):** 제안됨(Proposed)
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** T44, T9 #13, T10 #19, T11 #20, T31 #33, T38 #150, 설계 FR-3 · NFR-7 · 8.3

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** 설계는 "목적지는 Terraform과 공통 cloud-init으로 세운다(FR-3)", "모든 환경은 IaC로 생성·삭제(NFR-7)"다. 실제로 AWS 쪽은 Terraform이지만 목적지 온프렘 클러스터는 쉘·파이썬(`infra/envs/onprem`), 목적지 워크로드·시크릿·백업은 파이썬 렌더러와 kubectl(`target-workloads` 등)이다.
* **문제:** 운영 코드 관리가 나뉘어 있다(멘토링 지적). 버킷 공개 같은 목적지 설정 결함은 T31이 Secret을 직접 바꿔 고치므로 Git에 원하는 상태와 이력이 남지 않는다. 에이전트는 인프라를 읽기만 하고, 계획 단계에서 목적지 구성을 다룰 API가 없다.
* **목표:** 목적지 구성을 코드로 관리하고 API로 제공해, 에이전트가 계획 단계에서 호출할 수 있게 한다. 설계 원칙(제안은 AI, 실행은 코드, 에이전트에 실행 권한 없음)을 지킨다.

## 2. 고려한 옵션들 (Considered Options)

### 1. 플랫폼 plan/apply 서비스

기존 Terraform·렌더러를 감싼 서비스가 plan을 만들고, 승인된 plan만 apply한다.

**Pros**
* 빠르게 만들 수 있다. Terraform plan 파일 고정과 비슷한 흐름이다.

**Cons**
* 변경 이력이 플랫폼 원장에만 남는다. "운영 코드 관리" 지적에 직접 답하지 못한다.
* 적용 주체가 플랫폼 서비스라 권한이 한곳에 모인다.

### 2. GitOps: 원하는 상태는 Git, 플랫폼은 읽기·plan·제안만

모듈 카탈로그와 환경별 원하는 상태(`infra/iac/environments/*.json`)를 Git에 둔다. 플랫폼은 모듈 목록과 결정적 plan을 API로 준다. 에이전트는 `infra.plan`으로 계획만 한다. 사람이 PR을 검토·병합하면 적용기가 적용한다.

**Pros**
* 모든 변경이 PR·리뷰·병합 이력으로 남는다. 기존 GitOps(T43)·보호 규칙을 그대로 쓴다.
* 에이전트와 API에 쓰기·적용 권한이 없다. plan의 `authorizes_apply`는 항상 false다.

**Cons**
* 병합 뒤 적용기와 적용 검증을 따로 만들어야 한다.
* 비밀 값은 Git에 둘 수 없으므로 모듈은 형태만 갖고, 적용기가 적용 시점에 넣어야 한다.

### 3. 범용 IaC 도구 도입(Crossplane 등)

**Pros**
* 클러스터 안에서 선언형 조정(reconcile)을 바로 얻는다.

**Cons**
* 새 운영 구성요소와 권한이 늘고, 데모 일정 안에 인수할 수 없다.

## 3. 결정 사항 (Decision Outcome)

* **2. GitOps를 선택한다.** 첫 슬라이스는 목적지 첨부 버킷 공개 상태 모듈 하나다(`target-object-exposure` v1, T9 `object_exposure.py`와 같은 계정·권한, 시험으로 고정).
* **1. 플랫폼 plan/apply 서비스는 채택하지 않는다.** plan은 플랫폼이 주되 적용은 Git 병합을 거친다.
* **3. 범용 도구는 향후 확장 옵션으로 남긴다.** 적용기를 Argo CD로 할 때 다시 검토한다.

**이유**
* 지적된 문제가 "운영 코드 관리"이고, Git 이력과 리뷰가 그 답이다.
* 설계 8.3: 에이전트는 JSON으로 검증되는 제안만 내고 실행하지 않는다. `infra.plan`은 같은 입력에 같은 digest를 내는 순수 함수이며, 에이전트는 노출 확대(`IAC_WIDENING_REFUSED`)와 모듈 추가·삭제(`IAC_MODULE_SET_CHANGE_REFUSED`)를 계획할 수 없다.
* 에이전트 이미지는 IaC 라이브러리를 가져오지 않는다. 계획 포트(`bravo_iac.agent.AgentPlanner`)를 제어 경로가 주입한다(inventory와 같은 방식, 네트워크 없음).

### 네트워크 경로 (`tailnet-egress`)

* 해피패스에서 에이전트가 경로마다 들어가 Tailscale operator가 매긴 proxy 포트를 관찰하고 NetworkPolicy를 맞추느라 오래 걸렸다. 경로를 Git에 선언해 미리 깐다.
* **클러스터 쪽은 그 그룹 proxy Pod의 TCP 10000–10999를 연다.** 관찰 단계가 사라지는 대신, 기존 rehearsal-access의 "할당된 포트만 연다"보다 넓다. 그룹은 용도별 전용이고 목적지·포트는 tailnet grant가 정하므로, 열린 범위로 닿을 수 있는 것은 그 용도의 grant뿐이다.
* tailnet 정책 조각은 원하는 상태에서 생성하고 Tailscale 정책 `tests`로 불변식을 건다. 실검증은 Tailscale(`tailscale status`)로 한다.
* 이 맥의 첫 실행에서 목적지 API 장치 태그가 선언과 달랐다(`tag:bravo-target-operator` vs `tag:bravo-target-api`). 선언과 실제가 어긋나는 곳을 적용 전에 드러내는 것이 목적이다.

### 적용 위치: 고객 클러스터마다 Argo CD

* 플랫폼 Argo CD가 고객 클러스터들을 밀어 넣으면 플랫폼이 고객 클러스터마다 자격·API 경로를 쥐고, 고객이 늘수록 한곳에 부담과 권한이 모인다. **각 고객 클러스터에 Argo CD를 두고(pull), 그 클러스터의 환경 경로 한 곳만 끌어온다.**
* 원하는 상태는 환경별 Helm 차트로 생성한다. tailnet 접미사는 차트의 유일한 값이며 설치 때 Application에만 들어간다(공개 저장소에 두지 않는다).
* 권한은 1단계로 넉넉하게 시작한다(AppProject: 이 저장소·이 클러스터·모든 namespace·종류). 해피패스가 안정되면 README의 순서로 좁힌다. 팀 의견(먼저 넉넉하게, 이후 세분화)을 따른다.
* 단, **적용 커밋은 처음부터 고정한다.** develop은 승인 0이라 "사람이 검토·병합한 상태만 적용"이 병합만으로는 성립하지 않는다. 고객 클러스터는 설치 값 `targetRevision`(검토한 전체 커밋 SHA)만 적용하고, 승격은 그 값을 바꾸는 것이다(#592 리뷰 P1).
* 플랫폼 클러스터는 기존 T43 Argo에 `charts/platform`을 Application으로 붙인다. 고객용 설치 차트는 고객 클러스터 전용이다.
* 같은 자원의 주인은 하나다. 이 모듈을 적용하는 클러스터에서는 같은 경로를 만드는 기존 렌더러(rehearsal-access·executor-network·collector-network)를 쓰지 않고, 렌더러 경로는 담당 합의로 이 모듈로 옮긴다.

## 4. 결과

* `services/iac`(`bravo_iac`): 카탈로그, 원하는 상태 검사, plan, CLI(`modules`·`check`·`plan`·`stage`), 읽기·제안 API(`/v1/iac/modules`, `/v1/iac/environments/{id}`, `/v1/iac/plan`).
* `infra/iac/environments/target-dev.json`: 지금은 데모 1번의 심은 결함(`open-demo`, `demo_run_id`)이다.
* T11 `infra.plan`: 포트가 붙었을 때만 제공한다. 감사 기록에는 plan_digest·accepted·첫 거부 코드만 남긴다.
* 남은 일(T44 할 일): 실제 Job에 포트 연결(T38·T11), 플랫폼 API·PR 생성 연결(T10), 병합된 원하는 상태의 적용기와 적용 뒤 IAM-01.1 재대조(T31 또는 Argo CD).
* 설계 문서(FR-3·NFR-7·8.3)의 반영은 버전을 올려 따로 한다.
