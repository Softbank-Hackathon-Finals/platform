# [ADR-0033] 목적지를 처음 세우는 순서를 정형 단계 스택과 실행기로 제공

* **상태 (Status):** 제안됨(Proposed). 승인 위치(T10 `approval_kind=infra`), 설정 저장소, 승인 묶음 단위, T44(ADR-0032)와의 namespace 주인은 2026-11-07 정했다(4장). T30 단계 순서 확인이 남아 있다. 구현은 #604 · #620 · #634 · #642, 리뷰 후속(namespace · T44 Application 순서 · Keycloak)은 #700.
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** T45(신규), T44 · ADR-0032(목적지 IaC 모듈 · 원하는 상태 · 고객 클러스터 Argo CD), T30 #17, T9 #13, T11 #20, T10 #19, T43 #355, 설계 문서 8.8 · 8.11 "목적지 구축" 어댑터, ADR-0007 · 0019 · 0021 · 0024 · 0029

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** 2026-11-07 중간 점검에서 다음 네 가지가 지적됐다. ① 운영 쪽 절차가 코드로 관리되지 않는다. ② 클러스터를 펼치는 과정이 코드로 관리되지 않는다. ③ IaC가 없으니 IaC를 만들어 API로 제공해야 한다. ④ 에이전트가 계획을 세우는 과정을 정형화해 API로 호출할 수 있어야 한다.
* **문제:**
  * 코드로 관리되는 부분은 AWS 자원(Terraform), RKE2 노드 설치(cloud-init `bootstrap.sh`), 플랫폼 서비스 4개(Argo CD + Helm, ADR-0029)뿐이다.
  * 목적지 클러스터 위의 CNPG · SeaweedFS · Tailscale operator · OpenBao/ESO · 앱 · NetworkPolicy는 렌더러(`infra/envs/target-workloads/*.py`, `infra/envs/target-secrets/render.py` 등)의 출력을 운영자가 README 순서대로 `kubectl apply` · `helm install`한다. 렌더러는 출력만 하고 적용하지 않는다.
  * 순서, 선행 확인, 대기 조건, 비밀값 생성 위치가 README 문장에만 있다. 같은 절차를 다시 실행하면 사람마다 결과와 소요 시간이 달라지고, 처음부터 다시 구축하는 전체 시간은 측정된 적이 없다.
  * 설계 문서 8.11의 "목적지 구축 어댑터(Terraform · cloud-init으로 같은 기준의 목적지 생성)"는 구현되지 않았고 "데모에서는 사전 구축"으로 남아 있다. 심사 기준 "클라우드 활용"의 "IaC로 환경 생성" 항목과도 연결된다.
  * 에이전트 제안서(`Proposal`)는 정형화되어 있지만 작업 단위가 파일 교체(`edits`) 하나뿐이다. 인프라 작업을 표현하는 정형 단위가 없고, ADR-0007은 운영 IaC를 모델 수정 범위에서 제외한다.
* **목표:**
  * 목적지 구축 절차를 **선언형 스택 정의**로 고정하고, 같은 입력이면 같은 계획이 나오게 한다.
  * 선언형으로 표현되는 목적지 상태는 Git에 두고 **목적지 클러스터 안의 Argo CD**가 맞춘다. 플랫폼은 고객 클러스터의 상시 배포 주체가 되지 않는다.
  * 계획 생성 · 승인 · 적용 · 진행 조회를 **API**로 제공한다. 운영자, 콘솔, 에이전트가 같은 API를 쓴다.
  * 에이전트는 자유 텍스트나 셸 명령이 아니라 **스키마로 검증되는 계획 요청**만 낼 수 있고, 적용 권한은 갖지 않는다.
  * 단계별 소요 시간을 기록하고, 의존 관계가 없는 단계를 병렬로 실행해 재구축 시간을 줄인다.

### T44(ADR-0032)와의 관계

같은 날 같은 지적으로 이상무가 T44 "목적지 IaC API"를 먼저 만들었다(#592 · #593). 두 작업은 다음처럼 나눈다.

| | T44 (ADR-0032) | T45 (이 ADR) |
|---|---|---|
| 다루는 것 | 이미 선 목적지의 **설정**을 모듈과 원하는 상태(Git)로 관리한다(버킷 공개 상태, tailnet 경로) | 빈 클러스터에서 목적지를 **처음 세우는 순서**를 실행한다(DB → SeaweedFS → OpenBao/ESO → 앱) |
| 적용 | 병합된 원하는 상태를 고객 클러스터의 Argo CD가 끌어온다 | 같은 Argo CD에 자기 AppProject · Application을 더한다. 비밀값 생성, 사람 단계, 일회성 Job은 실행기가 직접 한다 |
| 목적지 Argo CD | `infra/iac/argocd` 설치(namespace `bravo-iac-gitops`)를 소유한다 | 설치하지 않는다. 그 설치가 있는지 확인만 한다 |
| 비밀값이 필요한 자원 | 차트에서 빼고 "다른 적용기 담당"으로 표시한다 | 그 "다른 적용기"를 맡는다(사람 단계 또는 `secret_generate`) |
| 에이전트 도구 | T11 `infra.plan`(모듈 변경 계획) | 이번 범위에서 새 도구를 만들지 않는다. 에이전트 신원은 API로 구축 계획을 만들고 조회만 할 수 있다 |

같은 자원의 주인은 하나다.

* **namespace와 그 보안 레이블은 T45**가 갖는다(`target-dev` 등, `tailscale-system` · `bravo-observability` 포함). T44 생성 chart는 namespace를 선언하지 않는다(#632 · #673).
* **tailnet 경로는 모두 T44**가 갖는다. #622부터 출발지 · 목적지의 DB · 제어 Pooler 노출과 복제 · Pooler egress도 T44 모듈이다. T45의 Tailscale 단계는 **operator 설치(`network.py values`)까지만** 한다.
* `target-dev/target-seaweedfs-auth` Secret은 T45 사람 단계가 만든다. **계정 · 권한의 형태**(`open-demo`/`closed`, `demo_run_id`)는 T44 원하는 상태(`target-dev.json`의 `target-object-exposure`)에서 가져오고, T45는 비밀값만 넣는다. 버킷 공개 상태를 바꾸는 일은 Git 한 곳(T44 PR)에서 이뤄지고, 처음 구축 뒤 닫기 · IAM-01.1 재대조는 T31 버킷 정책 분기를 재사용한다.
* T45 AppProject는 설정 저장소 하나와, 승인된 매니페스트가 만들거나 쓰는 namespace · 클러스터 범위 종류만 허용한다. T44 자원은 들어가지 않는다.

## 2. 고려한 옵션들 (Considered Options)

### 1. README 절차를 셸 스크립트 하나로 묶기

기존 렌더러 호출과 `kubectl apply`를 순서대로 실행하는 `bootstrap-target.sh`를 만든다.

**Pros**
* 가장 빨리 만들 수 있다.

**Cons**
* 계획과 적용이 분리되지 않아 승인 대상(무엇이 바뀌는지)을 미리 보여 줄 수 없다.
* 중간 실패 뒤 재실행 시 어디까지 적용됐는지 알 수 없고, 멱등성을 스크립트마다 따로 짜야 한다.
* API와 에이전트 연결이 불가능하다. 지적 ③ · ④를 해결하지 못한다.

### 2. 모든 단계를 Terraform(kubernetes · helm provider)으로 다시 작성

**Pros**
* 업계 표준 IaC이며 plan/apply가 기본으로 분리된다.

**Cons**
* 기존 렌더러가 담고 있는 검증 로직(`finalize-bao`의 readiness · 자원량 거부, 노드 이름 결속, 이미지 digest 고정)을 HCL로 다시 옮겨야 한다. T9 · T30에서 실측으로 고친 결함이 다시 생길 위험이 크다.
* 비밀값이 Terraform state에 남는다. "값은 메모리에서 Kubernetes로만 전달" 규칙(target-workloads · target-secrets README)과 충돌한다.
* OpenBao init처럼 사람이 복구 자료를 보관해야 하는 단계를 표현할 수 없다.

### 3. 기존 렌더러를 정형 단계로 감싼 스택 정의 + 계획 API가 직접 적용

스택 정의 파일이 단계 그래프를 선언하고, 계획 API가 렌더 · dry-run · 승인 결속 · 적용을 모두 직접 한다.

**Pros**
* 실측으로 검증된 렌더러를 그대로 재사용한다.
* 계획 → 승인 → 적용 구조가 ADR-0021(승인 결속 재계산)과 같은 원칙을 따른다.

**Cons**
* 적용 뒤 상태 유지(drift 교정)를 플랫폼이 계속 맡거나 아무도 맡지 않는다.
* 고객이 늘수록 플랫폼이 고객 클러스터마다 쓰기 자격을 오래 쥐게 된다.

### 4. 플랫폼 Argo CD(ADR-0029)가 목적지 자원까지 관리

**Pros**
* 이미 도입한 도구를 쓴다.

**Cons**
* 플랫폼 Argo CD 하나가 여러 고객 클러스터를 떠안는다. 플랫폼 장애나 권한 사고가 모든 고객 클러스터로 번진다.
* ADR-0029는 플랫폼 자체 서비스의 배포 결정이며, 고객 환경의 상시 제어 주체를 두는 결정이 아니다.
* 비밀값 생성, 호스트 디렉터리 준비, OpenBao init 같은 순서 의존 단계를 표현하기 어렵다.

### 5. 계획 API + 목적지 Argo CD 혼합 (선택)

옵션 3의 스택 정의 · 계획 · 승인 구조를 유지하되, **선언형 자원은 계획 API가 렌더해 목적지 설정 저장소에 커밋하고, 목적지 클러스터 안의 Argo CD가 동기화**한다. 계획 API가 직접 실행하는 것은 순서 의존 단계, 사람 개입 단계, 비밀값 생성, 일회성 Job, 목적지 Argo CD 최초 설치뿐이다.

**Pros**
* 적용 뒤 상태 유지(drift 교정)를 고객 클러스터 안의 Argo CD가 맡는다. 플랫폼 Argo CD가 고객 클러스터를 떠안지 않는다.
* 목적지 상태가 Git 커밋으로 남아, 승인한 계획과 실제 적용 상태를 커밋 SHA로 대조할 수 있다.
* 이전이 끝난 뒤 고객이 저장소와 Argo CD를 그대로 넘겨받을 수 있다.
* 렌더러와 후처리 검증(`finalize-bao` 등)은 커밋 전에 그대로 실행된다.

**Cons**
* 목적지마다 Argo CD 설치 자원과 비공개 설정 저장소가 하나씩 더 필요하다.
* 계획 API에 설정 저장소 쓰기 권한이 필요하다.
* 단계가 "API 직접"과 "Git → Argo" 두 경로로 나뉘어 구현이 옵션 3보다 넓다.

## 3. 결정 사항 (Decision Outcome)

* **옵션 5(계획 API + 목적지 Argo CD 혼합)를 선택한다.**
* **Terraform 스택(AWS 쪽 17개)의 일괄 적용은 같은 구조의 `terraform` 단계 종류로 2차 범위에 둔다.**
* **옵션 1 · 2 · 4는 채택하지 않는다. 옵션 3은 옵션 5에 흡수한다.**

**이유**
* 지적 ②③④를 한 구조로 해결한다: 스택 정의와 목적지 설정 저장소가 IaC이고, 계획 API가 그 IaC를 제공하며, 에이전트는 같은 API에 정형 요청만 보낸다.
* 고객 클러스터의 상시 배포 주체를 고객 클러스터 안에 둔다(2026-11-07 류지환 의견).
* 기존 렌더러의 실측 결과와 비밀값 규칙을 깨지 않는다.
* "AI는 제안, 판정 · 실행은 코드, 승인은 사람"이라는 팀 원칙(AGENTS.md)을 그대로 따른다.

### 3.1 구성

| 구성 | 위치(예정) | 역할 |
|---|---|---|
| 스택 정의 | `infra/stacks/target-onprem.json` | 단계 목록, 의존, 입력 파라미터 스키마, 담당 태스크. JSON Schema로 검증한다 |
| 계획 엔진 | `services/infra-plan` | 스택 + 환경 입력 → 렌더 → 설정 저장소 트리 digest · 서버 측 dry-run → 계획과 `plan_digest` |
| 적용 실행기 | `services/infra-plan` (별도 모듈) | T10 승인 확인 → digest 재계산 → 직접 단계 실행 · 설정 저장소 커밋 · 목적지 Argo CD 동기화 확인 |
| HTTP API | `services/infra-plan` | 아래 3.3. FastAPI(platform-events와 같은 방식). 플랫폼 배포는 T43(ADR-0029) 경로를 따른다 |
| 목적지 설정 저장소 | 목적지(고객)마다 비공개 Git 저장소 하나 | 렌더된 선언형 매니페스트. 비밀값은 넣지 않는다 |
| 목적지 Argo CD | T44 설치(`infra/iac/argocd`, namespace `bravo-iac-gitops`) | T45는 설치하지 않고 자기 AppProject · Application만 더한다. 설정 저장소를 읽기 전용으로 받아 동기화한다 |
| 에이전트 도구 | `services/agent-worker` (T11) | `infra.plan` 도구. 계획 생성 · 조회만 가능 |

계획 엔진과 적용 실행기는 LLM 클라이언트를 import하지 않는다.

### 3.2 단계 종류

| 종류 | 경로 | 하는 일 | 멱등 기준 |
|---|---|---|---|
| `probe` | API 직접 | 읽기 전용 확인. 노드 Ready, 클러스터 UID, Kubernetes 버전, 기존 자원 부재 등 | 항상 다시 실행 |
| `gitops_publish` | Git → Argo | 저장소의 렌더러를 고정 인자로 실행하고, Helm chart는 고정한 아카이브 SHA-256으로 `helm template` 뒤 후처리(예: `finalize-bao`)까지 마친 매니페스트를 설정 저장소의 정해진 경로에 커밋한다 | 렌더 트리가 설정 저장소의 현재 트리와 같으면 `SKIPPED` |
| `gitops_sync` | Git → Argo | 목적지 Argo CD Application이 해당 커밋 SHA로 `Synced`이고 `Healthy`인지 확인한다. 상한 시간이 있다 | 이미 그 SHA로 수렴했으면 즉시 통과 |
| `render_apply` | API 직접 | Argo CD가 맡기 어려운 일회성 자원(예: 마이그레이션 Job)만 렌더 → 서버 측 dry-run → `kubectl apply --server-side --field-manager=bravo-infra-plan` | dry-run 차이가 없거나 Job이 이미 성공했으면 `SKIPPED` |
| `secret_generate` | API 직접 | 정해진 형식의 비밀값을 실행기 메모리에서 생성해 Kubernetes API로 바로 만든다. Git · 계획 · 기록 · 로그에는 Secret 이름과 키 이름만 남긴다 | Secret이 이미 있으면 **덮어쓰지 않고** `SKIPPED` |
| `wait` | API 직접 | 조건 대기(DB Ready, Job Complete, ExternalSecret Ready). 상한 시간이 있다 | 조건이 이미 참이면 즉시 통과 |
| `operator_action` | 사람 | 사람만 할 수 있는 일(호스트 디렉터리 생성, 오프라인 이미지 반입, OpenBao init과 복구 자료 보관, Tailscale OAuth 입력, 설정 저장소 읽기 자격 등록). 실행기는 멈추고 지시문과 확인 조건을 보여 준다 | 확인 조건(`probe`)이 참이면 `SKIPPED` |
| `terraform` | API 직접 | 승인 때의 코드 · 모듈 · lock · 연결 정의 · 저장소 밖 backend/tfvars digest를 다시 대조 → `plan -out` → 삭제 · 교체가 있으면 `HOLD` → 아니면 그 plan 파일만 `apply`. 앞 스택 출력은 상태에서 읽어 메모리로만 넘긴다 | plan에 변경이 없으면 `SKIPPED` |

삭제 동작은 단계 종류에 없다. `helm_release`는 구현 중에 뺐다(`helm upgrade`는 chart에서 빠진 자원을 지우고, 적용 시점에 의존 chart를 다시 받는다). 목적지 Argo CD는 자동 prune을 끈 상태로 설치한다(ADR-0029와 같은 시작 조건). 설정 저장소에서 파일을 지워도 클러스터 자원은 지워지지 않는다. `helm uninstall`, `terraform destroy`, namespace · PV 삭제는 이 API로 실행할 수 없고 기존 수동 절차로 남긴다.

### 3.3 API

| 메서드 · 경로 | 호출자 | 하는 일 |
|---|---|---|
| `POST /v1/infra/plans` | 운영자, 콘솔, 에이전트 | 본문 `{stack, stack_revision, environment_id, params}`. 렌더 · dry-run 후 `PLANNED` 계획을 만든다. 클러스터와 설정 저장소에 쓰지 않는다 |
| `GET /v1/infra/plans/{id}` | 모두 | 단계별 렌더 digest, 설정 저장소 트리 차이 요약, dry-run 차이 요약, `operator_action` 목록, `plan_digest` |
| (승인) | 운영자(사람 SSO) | T10 `POST /v1/plans/{id}/approvals`에 `kind=infra`, binding `{infra_plan_id, plan_digest}`로 승인한다. 요청자와 승인자가 달라야 한다 |
| `POST /v1/infra/plans/{id}/runs` | 운영자 | 승인된 계획을 적용한다. `Idempotency-Key` 필수 |
| `GET /v1/infra/runs/{id}` | 모두 | 단계별 상태(`PENDING · RUNNING · SKIPPED · APPLIED · WAITING_OPERATOR · FAILED · HOLD`), 시작 · 종료 시각, 설정 저장소 커밋 SHA |
| `POST /v1/infra/runs/{id}/steps/{step}/confirm` | 운영자 | `operator_action`을 끝냈다고 알린다. 실행기는 확인 조건을 다시 검사한 뒤에만 진행한다 |

`plan_digest`는 정규화한 계획 요청, 스택 정의 digest, `stack_revision`(Git SHA), 대상 클러스터 UID와 노드 신원, 설정 저장소의 기준 커밋 SHA, 단계별 렌더 결과 digest를 합친 SHA-256이다. 비밀값은 포함하지 않는다.

### 3.4 적용 규칙

* 적용 직전에 T10에서 `kind=infra` 승인을 읽고, 계획을 다시 계산한다. `plan_digest`가 승인한 값과 다르면 `REFUSED`로 끝내고 아무것도 적용하지 않는다(ADR-0021과 같은 원칙).
* 설정 저장소의 기준 커밋이 계획 때와 달라졌으면(다른 사람이 먼저 커밋) `REFUSED`로 끝낸다. 실행기는 새 커밋만 만들고 강제 푸시하지 않는다.
* 의존 관계가 없는 단계는 병렬로 실행한다. 목적지 Argo CD에서도 줄기마다 Application을 나눠, DB · SeaweedFS · Tailscale · OpenBao/ESO가 서로 기다리지 않고 동기화된다.
* 한 단계가 실패하면 그 단계에 의존하는 단계는 시작하지 않는다. 이미 실행 중인 다른 줄기는 끝까지 진행한다.
* 결과를 확인할 수 없으면(타임아웃 뒤 상태 불명, Argo CD가 다른 SHA를 가리킴) 그 줄기를 `HOLD`로 멈추고 사람이 확인한다. 자동 재시도로 덮지 않는다.
* 같은 계획을 다시 실행하면 이미 수렴한 단계는 `SKIPPED`가 된다. 중간 실패 뒤 재실행이 처음부터 다시 하는 것보다 빨라야 한다.
* `gitops_sync`의 `Synced` · `Healthy`만으로 완료로 보지 않는다. 마지막 `probe` 단계의 실제 확인을 통과해야 실행이 끝난다.
* 단계 전이, 승인, 거부는 T24 감사 로그에 기록한다.

### 3.5 신원과 권한

1차는 해피패스를 먼저 통과시키기 위해 권한을 넉넉하게 두고, 해피패스가 안정된 뒤 세분화한다(2026-11-07 류지환 의견).

* 사람은 T10 승인 호스트와 같은 검증기(`operator_verifier`: 팀 IAM Identity Center 증명 또는 고정 OIDC 토큰)로 확인하고, 같은 토큰을 T10에 넘긴다. 실행 시작과 재개마다 T10이 그 사람의 `apply_infra`와 승인 회수를 다시 확인한다.
* 실행은 모든 단계 전에 대상(클러스터 · 노드 UID, AWS 스택은 STS 계정)을 다시 확인하고, kube context를 시작 때 고정한다.
* 계획 엔진은 환경 등록(T10 `connection_ref`)의 **읽기 전용** 신원으로 dry-run과 상태 조회만 한다.
* 적용 실행기는 1차에서 목적지 클러스터 관리자 수준의 적용 신원을 실행마다 받는다. 상시 보관하지 않고 실행 시간과 대상 클러스터 UID로 제한한다. 세분화(delete 제외, namespace 한정, 목적지 Argo CD로 쓰기 이전)는 T45의 후속 고도화로 남긴다.
* 설정 저장소 쓰기는 그 저장소의 정해진 브랜치 하나로 제한한 자격을 쓴다. 목적지 Argo CD는 같은 저장소의 읽기 전용 자격만 받는다.
* 목적지 Argo CD는 UI · Dex · admin 계정 없이 설치하고(플랫폼 Argo CD 값과 같은 기준), AppProject로 대상 namespace와 리소스 종류를 제한한다. 플랫폼은 목적지 Argo CD의 관리자 자격을 보관하지 않는다.
* **에이전트 신원은 넉넉한 권한 적용 대상이 아니다.** `POST /v1/infra/plans`와 조회만 할 수 있고, 승인 · 적용 · 확인 경로는 API가 신원 종류로 거부한다(프롬프트가 아니라 API에서 막는다). **신원 종류는 검증된 자격(T10 운영자 신원, 워크로드 신원)에서만 얻고, 요청 헤더 · 본문의 값은 쓰지 않는다.** 사람과 구별되는 에이전트 워크로드 신원(T38)이 연결되기 전까지는 에이전트를 API에 아예 들이지 않는다.

### 3.6 에이전트 연결

* 에이전트 신원은 `POST /v1/infra/plans`로 구축 계획을 만들고 조회할 수 있다. 요청 본문은 계획 요청 JSON Schema와 같고, 스택이 허용한 `params`만 받는다. T11의 `infra.plan` 도구 이름은 T44(모듈 변경 계획)가 쓰므로, 구축 계획 도구가 필요해지면 다른 이름으로 따로 둔다(이번 범위 밖).
* 에이전트가 바꿀 수 있는 것은 파라미터뿐이다. 스택 정의, 렌더러, 단계 종류, 설정 저장소 내용은 모델의 수정 범위가 아니다(ADR-0007의 운영 IaC 제외 규칙 유지).
* 에이전트가 만든 계획도 사람이 T10에서 승인해야 적용된다. 에이전트 계획의 `requested_by`에는 실행 ID와 시도 번호를 결속한다.

### 3.7 첫 스택: `target-onprem`

현재 README 절차를 그대로 옮긴 순서다. 렌더러 인자와 버전은 각 README와 코드의 고정값을 따른다. "경로"의 Git은 `gitops_publish` → `gitops_sync`를 뜻한다.

| 순서 | 단계 | 종류 · 경로 | 기존 절차 | 담당 |
|---|---|---|---|---|
| 1 | 클러스터 · 노드 신원, 새 환경 여부 확인 | `probe` | target-workloads README 1 | T30 |
| 2 | 호스트 디렉터리(DB UID 26, objects UID 1000, OpenBao 100:1000 0700, 로그인 서버 `login` UID 1000) | `operator_action` | target-workloads 2 · 로그인 서버 1, target-secrets 3 | T30 · T9 |
| 3 | T44 목적지 Argo CD chart 0.2.x를 `application.enabled=false`(autoSync · prune false)로 한 번 설치해 컨트롤러와 CRD만 확인, 설정 저장소 읽기 자격 등록 | `operator_action` | `infra/iac/README.md` 설치 절차 1단계 | T44 · T45 |
| 4 | namespace(`target-dev` · OpenBao · ESO · `tailscale-system` · `bravo-observability`), 서비스 계정 | Git | target-workloads 3 · 5 (`render.py accounts`), `infra/iac/README.md` namespace 주인 표 | T30 · T37 |
| 5 | DB 비밀값 `target-app-db`, `target-replication`, SeaweedFS `s3.json` | `secret_generate` | target-workloads 3, 개발용 첨부 저장소 | T30 · T9 |
| 6 | CNPG operator chart 0.29.1 → 목적지 DB → Ready 대기 | Git · `wait` | target-workloads 4 · 5 | T30 |
| 7 | 마이그레이션 Job → Complete 대기 | `render_apply` · `wait` | target-workloads 6 | T30 |
| 8 | SeaweedFS (`objects.py`) | Git | target-workloads 개발용 첨부 저장소 | T30 · T9 |
| 9 | Tailscale OAuth 입력 → operator chart 1.104.4 | `operator_action` · Git | target-workloads 분리된 네트워크 경로 | T30 |
| 9a | T44 Application 켜기(`helm upgrade ... --set application.enabled=true`, autoSync · prune false 유지) → T44 판정기 `customer_status`로 계획의 `iac_revision`을 가리키는지 확인 | `operator_action` · `probe` | `infra/iac/README.md` 설치 절차 2단계(#673 순서 계약) | T44 |
| 10 | OpenBao 오프라인 이미지 반입, TLS 자료 생성과 보관 | `operator_action` | target-secrets 2 · 4 | T9 |
| 11 | OpenBao foundation · chart(`finalize-bao` 후처리) | Git | target-secrets 3 · 5 | T9 |
| 12 | ESO chart, OpenBao · ESO NetworkPolicy | Git | target-secrets 6 | T9 |
| 13 | OpenBao init과 복구 자료 보관, KV · auth · policy | `operator_action` | target-secrets 7 · 8 | T9 |
| 14 | `delivery` → ExternalSecret Ready 대기 | Git · `wait` | target-secrets 9 | T9 |
| 15 | 목적지 앱 | Git | target-workloads 개발 측정용 앱 (`application.py`) | T30 |
| 15a | 로그인 서버(Keycloak `target-login`, #652): 관리자 Secret `target-login-admin` → `identity.py` → Ready → Edge 로그인 경로(`edge-cloudflare` `hostnames.login`) | `secret_generate` · Git · `operator_action` | target-workloads 목적지 로그인 서버, edge-cloudflare 목적지 로그인 주소 | T30 · T17 |
| 16 | 구축 결과 확인(로그인 서버가 있으면 공개 주소 탐침 포함) | `probe` (`probes.py`, `verify.py`) | 각 README 확인 절차 | T30 · T9 |

5 이후 DB(6~7), SeaweedFS(8), Tailscale(9 · 9a), OpenBao/ESO(10~14), 로그인 서버(15a)는 병렬 줄기다. 15는 7 · 8 · 14 뒤에 실행하고, 16은 15 · 9a · 15a 뒤에 실행한다.

**조건부 단계와 파라미터.** `tailnet=none`(같은 클러스터 안 격리 시험)은 9 · 9a를, `login=none`(기본값, Edge 없는 격리 시험과 T49 무인 구축)은 15a를 계획에서 뺀다. 로그인 줄기는 `login=edge`를 준 계획만 계획한다. T44 Application은 Tailscale CRD가 있어야 하므로 9a는 tailnet 줄기에만 있다. `iac_revision`(T44 설치 값 `targetRevision`)은 `tailnet=join`일 때, `login_public_url` · `app_origin`은 `login=edge`일 때만 필수다(파라미터의 `required_when`). 조건부 필수 값은 같은 조건으로 계획되는 단계에서만 쓸 수 있고, 스택 검사가 이를 강제한다.

**겹치는 주인(#700).** `bravo-observability` Namespace는 T37 Collector 매니페스트(`services/observability/deploy/collector/resources.yaml`, 목적지는 `collector-target`이 이를 포함)도 선언한다. T45 base는 같은 레이블(`component: platform`, `tier`, `role`, `domain`, PSA `restricted`)로 선언해 어느 쪽이 적용해도 값이 바뀌지 않게 하고, 시험이 두 원문의 레이블 일치를 고정한다. 주인을 하나로 하려면 T37 매니페스트에서 Namespace를 빼야 하며 T37 담당과 합의한다.

### 3.8 두 번째 스택: `target-onprem-migration` (#648)

`target-onprem`으로 세운 목적지가 정방향 복제의 초기 복사를 끝낼 때까지의 순서다. 같은 실행기 · 계획 digest · `approval_kind=infra` 승인을 쓴다. 실행기는 AWS 플랫폼 RKE2에서 돌고, 대상은 온프렘 목적지 클러스터다(해피패스: AWS EKS 출발지 → 온프렘 RKE2 목적지, 11/7 사용자 기준). 목적지에는 플랫폼 구성요소를 두지 않는다. 목적지에서 관찰할 수 있는 것만 둔다. 출발지의 역방향 준비와 T10 전이는 이 스택 밖이다(T10이 REHEARSE 전에 이 실행이 SUCCEEDED인지 읽는다).

| 순서 | 단계 | 종류 | 담당 |
|---|---|---|---|
| 1 | 클러스터 · 노드 신원 | `probe` | T30 |
| 2 | 노드 감사 정책(#645 묶음) — Metadata 감사 이벤트 | `operator_action` | T44 |
| 3 | T44 네트워크 Application 수렴 — T44 판정기 `customer_status`(#635) | `operator_action` | T44 |
| 4 | `target-db`의 외부 클러스터 `t8-source` 선언 확인 | `probe` | T8 |
| 5 | 복제 자격 Secret, CNPG 적재(`cnpg.io/reload`) | `operator_action` | T8 |
| 6 | 목적지 표가 있고 비어 있음 | `probe` | T8 |
| 7 | 정방향 구독 생성(없을 때만) | `render_apply` (`create_only`) | T8 |
| 8 | 초기 복사 완료 | `operator_action` | T8 |

* **외부 클러스터의 주인은 database 렌더다.** `target-db`는 3.7의 6단계 Argo 앱(selfHeal)이 소유한다. 그래서 외부 클러스터는 T30 `render.py database`가 Cluster 안에 선언하고, 이 스택은 확인만 한다. 명령형 패치는 주인을 둘로 만들어 Argo가 되돌리거나 상태가 흔들린다(2026-11-07 실측). 주소는 T44 egress Service의 kube DNS 이름이고 관찰한 IP(`hostaddr`)를 쓰지 않는다. 그래서 출발지 인증서가 그 이름을 SAN으로 가진다.
* **롤아웃 순서.** 이미 정방향 복제가 도는 목적지에 이 렌더를 반영하려면 **출발지 인증서를 새 SAN으로 먼저 다시 발급한다.** 그 뒤 목적지를 다시 렌더한다. 순서가 거꾸로 되면 CNPG가 구독 연결을 새 host로 바꾸고, verify-full이 실패해 정방향 복제가 끊긴다. 이 스택은 구독이 이 이유로 연결하지 못하면 `source_tls_name_mismatch`로 멈춘다.
* **구독은 만들기만 한다.** 없을 때만 `kubectl create`로 만든다. 있고 계획과 같으면 SKIPPED, 다르면 HOLD다. 켜고 끄기(SQL ENABLE/DISABLE), `disable_on_error` 조정, 역방향은 T8 · T12가 맡는다.
* **복제 자격은 라벨이 필요하다.** Cluster보다 늦게 만든 Secret은 `cnpg.io/reload=true`가 없으면 CNPG가 읽지 않는다(CNPG 1.30.1 실측). 자격 확인은 Cluster 상태의 `externalClusterSecretVersion`까지 본다. 나중에 이 Secret을 ESO나 `secret_generate`로 옮기면 그 템플릿에도 라벨을 둔다.
* **판정의 위치.** 복제 판정(볼 대상, 고정 읽기 쿼리, 판정)은 `bravo_infra_plan/replication.py`, Argo 수렴 판정은 T44 `infra/iac/customer_status.py`를 그대로 실행한다.

## 4. 결과

### 좋아지는 점
* 목적지 구축 절차가 README 문장에서 검증 가능한 스택 정의로 바뀐다. 순서, 대기 조건, 사람 개입 지점이 코드에 고정된다.
* 목적지의 선언형 상태가 고객 소유 저장소의 커밋으로 남고, 고객 클러스터 안의 Argo CD가 그 상태를 유지한다.
* 운영자 · 콘솔 · 에이전트가 같은 API로 계획을 만들고, 사람이 T10에서 digest를 승인해야만 적용된다.
* 단계별 소요 시간이 기록되어 재구축 시간을 처음으로 측정할 수 있고, 병렬 줄기와 멱등 재실행으로 그 시간을 줄인다.

### 감수하는 점
* 렌더러의 CLI 인자가 스택 정의와 결합된다. 렌더러 인자를 바꾸는 PR은 스택 정의도 함께 바꿔야 하며, CI에서 스택의 렌더 단계를 실제로 실행해 깨짐을 잡는다.
* 목적지마다 Argo CD와 비공개 설정 저장소가 하나씩 늘어난다. 목적지 노드의 CPU · 메모리 여유 안에서 설치 크기를 정하고 증가량을 기록한다.
* `operator_action` 단계는 여전히 사람이 한다. 이 ADR은 그 단계를 없애는 결정이 아니라, 코드가 그 단계를 알고 멈추게 하는 결정이다.
* 1차 적용 신원의 넓은 권한은 잔여 위험이다. 실행 단위로만 발급하고, 세분화는 후속 고도화로 추적한다.

### 합의된 점 (2026-11-07)
* 승인은 T10 approvals에 `approval_kind=infra`를 추가해 한곳으로 모은다(류지환 동의).
* 선언형 배포는 고객 클러스터 안의 Argo CD가 맡는다. 플랫폼 Argo CD가 고객 클러스터를 떠안지 않는다(류지환 의견). 그 Argo CD는 T44가 설치한 것을 함께 쓴다.
* 권한은 1차에 넉넉하게 두고 해피패스 뒤에 세분화한다(류지환 의견).
* 목적지 설정 저장소는 조직 안의 비공개 저장소로 새로 만든다. 공개 저장소인 `platform`에는 노드 이름 같은 운영 식별자가 들어간 렌더 결과를 두지 않는다(전동훈 결정).
* `infra` 승인 종류는 #603으로 T10에 들어갔다(PR 제목은 당시 번호 T44). T10 코드는 승인 종류 추가 범위만 바꿨다.
* 목적지 구축 승인은 그 목적지로 옮기는 이전 계획(`/v1/plans/{plan_id}`)에 묶는다. 컷오버 전 단계(COLLECT ~ AWAIT_APPROVAL)에서만 승인할 수 있다.
* **목적지 namespace와 보안 레이블의 주인은 T45다**(16:33 이상무, T44 #632). T44 생성 chart는 namespace를 선언하지 않는다. T45 base가 `tailscale-system`(Pod Security `privileged`)과 `bravo-observability`(T37 Collector와 같은 레이블, PSA `restricted`, #673 T37 동의)도 만든다.
* **T44 Application은 T45 base · tailscale이 Synced · Healthy가 된 뒤에 켠다**(#673). 그래서 3단계는 `application.enabled=false`로 컨트롤러만 설치하고, 9a가 tailscale-sync 뒤에 켠다. 자동 동기화 승격(`autoSync=true`)은 T44 절차이며 이 스택이 하지 않는다(#635 · #677).
* `object-secrets`는 T44 원하는 상태(`target-object-exposure`)의 값을 계획에 결속하고, 확인 조건이 Secret 모양을 그 상태와 대조한다.
* 실행 기록은 T24 해시 체인(source `infra`, #639)에 남긴다.

### 팀 확인이 필요한 점
1. **T30(김태현) · T9 렌더러 재사용 범위**: 렌더러 코드는 바꾸지 않고 CLI로만 호출한다. 예외는 3.8의 외부 클러스터 선언이다(#648, T30 동의 필요). 3.7 표의 T30 단계 순서가 실제 구축 순서와 같은지 확인을 요청한다.
2. **Terraform 실측**: `infra/stacks/aws-platform`(13개 스택)과 `terraform` 단계는 구현했다(#634). 실제 AWS에서는 `platform-audit` 하나만 읽기 전용 `verify`로 `NO_CHANGES`를 확인했다. 나머지 12개는 담당(류지환)의 개인 backend · tfvars로 `verify`를 돌려야 한다.

### 완료 증거

지금까지 확인한 것은 `docs/evidence/t45-kind-run-2026-11-07`(kind 2회, T44 Argo CD, 실제 T10 검증기 · 권한)과 #634의 실제 AWS `verify`에 있다. 아래는 남은 기준이다.

1. `local-vm.sh up`으로 만든 새 VM에서 API로 계획 → T10 승인 → 적용 → 16단계 확인 통과. 단계별 시각과 전체 소요 시간을 기록한다.
2. 목적지 Argo CD의 Application revision이 실행 기록의 설정 저장소 커밋 SHA와 같다.
3. 같은 계획을 다시 적용하면 모든 단계가 `SKIPPED`이고 새 커밋과 클러스터 변경이 없다.
4. 중간 단계를 일부러 실패시킨 뒤 재실행하면 실패한 단계부터 이어진다.
5. 렌더러 출력이나 설정 저장소 기준 커밋을 바꾼 상태에서 이전 승인으로 적용하면 `REFUSED`가 된다.
6. 에이전트 신원으로 승인 · 적용을 호출하면 거부되고 감사 기록이 남는다.
7. 비밀값이 설정 저장소 · 계획 · 실행 기록 · 로그 · 응답 어디에도 없다(`public_evidence.py check`, 값 검색).
