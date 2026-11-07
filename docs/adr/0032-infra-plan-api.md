# [ADR-0032] 목적지 구축을 정형 계획 API로 제공

* **상태 (Status):** 제안됨(Proposed). 팀 합의 전이며 구현은 시작하지 않았다.
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** T44(신규), T30 #17, T9 #13, T11 #20, T10 #19, T43 #355, 설계 문서 8.8 · 8.11 "목적지 구축" 어댑터, ADR-0007 · 0019 · 0021 · 0024 · 0029

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
  * 계획 생성 · 승인 · 적용 · 진행 조회를 **API**로 제공한다. 운영자, 콘솔, 에이전트가 같은 API를 쓴다.
  * 에이전트는 자유 텍스트나 셸 명령이 아니라 **스키마로 검증되는 계획 요청**만 낼 수 있고, 적용 권한은 갖지 않는다.
  * 단계별 소요 시간을 기록하고, 의존 관계가 없는 단계를 병렬로 실행해 재구축 시간을 줄인다.

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

### 3. 기존 렌더러를 정형 단계로 감싼 스택 정의 + 계획 API (선택)

스택 정의 파일이 단계 그래프를 선언하고, 각 단계는 정해진 **단계 종류**(아래 3.2) 중 하나다. 계획 엔진이 렌더러를 호출해 결과를 만들고 서버 측 dry-run으로 차이를 계산한 뒤 계획 digest를 낸다. 적용 실행기는 승인된 digest와 다시 계산한 digest가 같을 때만 적용한다.

**Pros**
* 실측으로 검증된 렌더러를 그대로 재사용한다. 렌더러 코드는 바꾸지 않는다.
* 계획 → 승인 → 적용 구조가 ADR-0021(승인 결속 재계산)과 같은 원칙을 따른다.
* 사람이 해야 하는 단계도 `operator_action`으로 그래프 안에 넣어, 어디서 멈추고 무엇을 확인해야 하는지 코드가 안다.
* 같은 계획 요청 스키마가 에이전트 도구의 입력이 된다.

**Cons**
* 단계 종류와 실행기를 새로 만들어야 한다.
* Helm으로 설치하는 operator(CNPG · ESO · Tailscale)는 클러스터 범위 권한이 필요하다. 적용 신원의 권한을 좁히는 데 한계가 있다(3.5).

### 4. Argo CD(ADR-0029)로 목적지 자원까지 관리

**Pros**
* 이미 도입한 도구를 쓴다.

**Cons**
* ADR-0029는 플랫폼 자체 서비스의 배포 결정이며, 목적지(고객 환경)에 플랫폼의 상시 제어 주체를 두는 결정이 아니다.
* 비밀값 생성, 호스트 디렉터리 준비, OpenBao init 같은 순서 의존 단계를 표현하기 어렵다.
* 이전이 끝난 뒤 목적지를 고객이 소유해야 하는 제품 방향과 맞지 않는다.

## 3. 결정 사항 (Decision Outcome)

* **옵션 3(정형 단계 스택 + 계획 API)을 선택한다.**
* **Terraform 스택(AWS 쪽 17개)의 일괄 적용은 같은 구조의 `terraform` 단계 종류로 2차 범위에 둔다.**
* **옵션 1 · 2 · 4는 현 단계에서 채택하지 않는다.**

**이유**
* 지적 ②③④를 한 구조로 해결한다: 스택 정의가 IaC이고, 계획 API가 그 IaC를 제공하며, 에이전트는 같은 API에 정형 요청만 보낸다.
* 기존 렌더러의 실측 결과와 비밀값 규칙을 깨지 않는다.
* "AI는 제안, 판정 · 실행은 코드, 승인은 사람"이라는 팀 원칙(AGENTS.md)을 그대로 따른다.

### 3.1 구성

| 구성 | 위치(예정) | 역할 |
|---|---|---|
| 스택 정의 | `infra/stacks/target-onprem.json` | 단계 목록, 의존, 입력 파라미터 스키마, 담당 태스크. JSON Schema로 검증한다 |
| 계획 엔진 | `services/infra-plan` | 스택 + 환경 입력 → 렌더 → 서버 측 dry-run → 계획과 `plan_digest` |
| 적용 실행기 | `services/infra-plan` (별도 모듈) | 승인 확인 → digest 재계산 → 의존 그래프 순서로 적용 · 대기 · 확인 |
| HTTP API | `services/infra-plan` | 아래 3.3. FastAPI(platform-events와 같은 방식) |
| 에이전트 도구 | `services/agent-worker` (T11) | `infra.plan` 도구. 계획 생성 · 조회만 가능 |

계획 엔진과 적용 실행기는 LLM 클라이언트를 import하지 않는다.

### 3.2 단계 종류

| 종류 | 하는 일 | 멱등 기준 |
|---|---|---|
| `probe` | 읽기 전용 확인. 노드 Ready, 클러스터 UID, Kubernetes 버전, 기존 자원 부재 등 | 항상 다시 실행 |
| `render_apply` | 저장소의 렌더러를 고정 인자로 실행 → 서버 측 dry-run → `kubectl apply --server-side --field-manager=bravo-infra-plan` | dry-run 차이가 없으면 `SKIPPED` |
| `helm_release` | 코드에 고정한 chart 아카이브 SHA-256과 values로 설치. 렌더 후처리(예: `finalize-bao`)가 있으면 그 결과만 적용 | 같은 chart · values digest의 release가 있으면 `SKIPPED` |
| `secret_generate` | 정해진 형식의 비밀값을 실행기 메모리에서 생성해 Kubernetes API로 바로 만든다. 계획 · 기록 · 로그에는 Secret 이름과 키 이름만 남긴다 | Secret이 이미 있으면 **덮어쓰지 않고** `SKIPPED` |
| `wait` | 조건 대기(Ready, Job Complete, ExternalSecret Ready). 상한 시간이 있다 | 조건이 이미 참이면 즉시 통과 |
| `operator_action` | 사람만 할 수 있는 일(호스트 디렉터리 생성, 오프라인 이미지 반입, OpenBao init과 복구 자료 보관, Tailscale OAuth 입력). 실행기는 멈추고 지시문과 확인 조건을 보여 준다 | 확인 조건(`probe`)이 참이면 `SKIPPED` |
| `terraform` (2차) | 고정 revision에서 `plan -out` → 승인 → 그 plan 파일만 `apply`. 앞 스택의 `output -json`을 다음 단계 입력으로 넘긴다 | plan에 변경이 없으면 `SKIPPED` |

삭제 동작은 단계 종류에 없다. prune, `helm uninstall`, `terraform destroy`, namespace · PV 삭제는 이 API로 실행할 수 없고 기존 수동 절차로 남긴다.

### 3.3 API

| 메서드 · 경로 | 호출자 | 하는 일 |
|---|---|---|
| `POST /v1/infra/plans` | 운영자, 콘솔, 에이전트 | 본문 `{stack, stack_revision, environment_id, params}`. 렌더 · dry-run 후 `PLANNED` 계획을 만든다. 클러스터에 쓰지 않는다 |
| `GET /v1/infra/plans/{id}` | 모두 | 단계별 렌더 digest, dry-run 차이 요약, `operator_action` 목록, `plan_digest` |
| `POST /v1/infra/plans/{id}/approvals` | 운영자(사람 SSO) | 본문 `{plan_digest}`. 계획 요청자와 다른 사람만 승인할 수 있다 |
| `POST /v1/infra/plans/{id}/runs` | 운영자 | 승인된 계획을 적용한다. `Idempotency-Key` 필수 |
| `GET /v1/infra/runs/{id}` | 모두 | 단계별 상태(`PENDING · RUNNING · SKIPPED · APPLIED · WAITING_OPERATOR · FAILED · HOLD`)와 시작 · 종료 시각 |
| `POST /v1/infra/runs/{id}/steps/{step}/confirm` | 운영자 | `operator_action`을 끝냈다고 알린다. 실행기는 확인 조건을 다시 검사한 뒤에만 진행한다 |

`plan_digest`는 정규화한 계획 요청, 스택 정의 digest, `stack_revision`(Git SHA), 대상 클러스터 UID와 노드 신원, 단계별 렌더 결과 digest를 합친 SHA-256이다. 비밀값은 포함하지 않는다.

### 3.4 적용 규칙

* 적용 직전에 계획을 다시 계산한다. `plan_digest`가 승인한 값과 다르면 `REFUSED`로 끝내고 아무것도 적용하지 않는다(ADR-0021과 같은 원칙).
* 의존 관계가 없는 단계는 병렬로 실행한다. 예: 공통 namespace · 계정 이후 DB 줄기, SeaweedFS 줄기, OpenBao/ESO 줄기, Tailscale 줄기는 서로 기다리지 않는다.
* 한 단계가 실패하면 그 단계에 의존하는 단계는 시작하지 않는다. 이미 실행 중인 다른 줄기는 끝까지 진행한다.
* 결과를 확인할 수 없으면(타임아웃 뒤 상태 불명) 그 줄기를 `HOLD`로 멈추고 사람이 확인한다. 자동 재시도로 덮지 않는다.
* 같은 계획을 다시 실행하면 이미 수렴한 단계는 `SKIPPED`가 된다. 중간 실패 뒤 재실행이 처음부터 다시 하는 것보다 빨라야 한다.
* 단계 전이, 승인, 거부는 T24 감사 로그에 기록한다.

### 3.5 신원과 권한

* 계획 엔진은 환경 등록(T10 `connection_ref`)의 **읽기 전용** 신원으로 dry-run만 한다.
* 적용 실행기는 실행마다 만료 시간이 있는 적용 신원을 받는다. 상시 보관하지 않는다. 적용 신원에는 delete 권한을 주지 않는다.
* operator 설치에 필요한 클러스터 범위 권한(CRD, ClusterRole, Namespace, PV)은 피할 수 없다. 이 권한이 적용 신원에 있다는 점을 잔여 위험으로 남기고, 실행 시간과 대상 클러스터 UID로 제한한다.
* 에이전트 신원은 `POST /v1/infra/plans`와 조회만 할 수 있다. 승인 · 적용 · 확인 경로는 API가 신원 종류로 거부한다(프롬프트가 아니라 API에서 막는다).

### 3.6 에이전트 연결

* T11에 `infra.plan` 도구를 추가한다. 인자는 계획 요청 JSON Schema와 같고, 스택이 허용한 `params`만 받는다.
* 에이전트가 바꿀 수 있는 것은 파라미터뿐이다. 스택 정의, 렌더러, 단계 종류는 모델의 수정 범위가 아니다(ADR-0007의 운영 IaC 제외 규칙 유지).
* 에이전트가 만든 계획도 사람이 승인해야 적용된다. 에이전트 계획의 `requested_by`에는 실행 ID와 시도 번호를 결속한다.

### 3.7 첫 스택: `target-onprem`

현재 README 절차를 그대로 옮긴 순서다. 렌더러 인자와 버전은 각 README와 코드의 고정값을 따른다.

| 순서 | 단계 | 종류 | 기존 절차 | 담당 |
|---|---|---|---|---|
| 1 | 클러스터 · 노드 신원, 새 환경 여부 확인 | `probe` | target-workloads README 1 | T30 |
| 2 | 호스트 디렉터리(DB UID 26, objects UID 1000, OpenBao 100:1000 0700) | `operator_action` | target-workloads 2, target-secrets 3 | T30 · T9 |
| 3 | `target-dev` namespace, 서비스 계정 | `render_apply` (`render.py accounts`) | target-workloads 3 · 5 | T30 |
| 4 | DB 비밀값 `target-app-db`, `target-replication` | `secret_generate` | target-workloads 3 | T30 |
| 5 | CNPG operator chart 0.29.1 | `helm_release` | target-workloads 4 | T30 |
| 6 | 목적지 DB → Ready 대기 → 마이그레이션 Job → Complete 대기 | `render_apply` · `wait` | target-workloads 5 · 6 | T30 |
| 7 | SeaweedFS `s3.json` → `objects.py` | `secret_generate` · `render_apply` | target-workloads 개발용 첨부 저장소 | T30 · T9 |
| 8 | Tailscale OAuth 입력 → operator chart 1.104.4 | `operator_action` · `helm_release` | target-workloads 분리된 네트워크 경로 | T30 |
| 9 | OpenBao 오프라인 이미지 반입 | `operator_action` | target-secrets 2 | T9 |
| 10 | OpenBao foundation · TLS · chart(`finalize-bao`) | `render_apply` · `operator_action` · `helm_release` | target-secrets 3~5 | T9 |
| 11 | ESO chart, OpenBao · ESO NetworkPolicy | `helm_release` · `render_apply` | target-secrets 6 | T9 |
| 12 | OpenBao init과 복구 자료 보관, KV · auth · policy | `operator_action` | target-secrets 7 · 8 | T9 |
| 13 | `delivery` → ExternalSecret Ready 대기 | `render_apply` · `wait` | target-secrets 9 | T9 |
| 14 | 목적지 앱 | `render_apply` (`application.py`) | target-workloads 개발 측정용 앱 | T30 |
| 15 | 구축 결과 확인 | `probe` (`probes.py`, `verify.py`) | 각 README 확인 절차 | T30 · T9 |

3 이후 DB(4~6), SeaweedFS(7), Tailscale(8), OpenBao/ESO(9~13)는 병렬 줄기다. 14는 6 · 7 · 13 뒤에 실행한다.

## 4. 결과

### 좋아지는 점
* 목적지 구축 절차가 README 문장에서 검증 가능한 스택 정의로 바뀐다. 순서, 대기 조건, 사람 개입 지점이 코드에 고정된다.
* 운영자 · 콘솔 · 에이전트가 같은 API로 계획을 만들고, 사람이 digest를 승인해야만 적용된다.
* 단계별 소요 시간이 기록되어 재구축 시간을 처음으로 측정할 수 있고, 병렬 줄기와 멱등 재실행으로 그 시간을 줄인다.

### 감수하는 점
* 렌더러의 CLI 인자가 스택 정의와 결합된다. 렌더러 인자를 바꾸는 PR은 스택 정의도 함께 바꿔야 하며, CI에서 스택의 렌더 단계를 실제로 실행해 깨짐을 잡는다.
* `operator_action` 단계는 여전히 사람이 한다. 이 ADR은 그 단계를 없애는 결정이 아니라, 코드가 그 단계를 알고 멈추게 하는 결정이다.
* 적용 신원의 클러스터 범위 권한은 잔여 위험이다.

### 팀 확인이 필요한 점
1. **T30(김태현) · T9 렌더러 재사용 범위**: 렌더러 코드는 바꾸지 않고 CLI로만 호출한다. 스택 정의의 T30 단계 순서가 README와 같은지 확인을 요청한다.
2. **승인 위치**: 1차는 이 API 안에서 승인(요청자 ≠ 승인자)하고 T24에 기록한다. T10 `approvals`에 `kind=infra`를 추가해 승인을 한곳으로 모을지는 T10 담당(류지환)과 정한다.
3. **서비스 배포**: `services/infra-plan`의 플랫폼 배포는 T43(ADR-0029) 경로를 따르는 것을 전제로 한다.
4. **Terraform 2차 범위**: AWS 스택 일괄 적용은 T1 · T6 · T7 담당(류지환)과 범위를 합의한 뒤 시작한다.

### 완료 증거(예정)
1. `local-vm.sh up`으로 만든 새 VM에서 API로 계획 → 승인 → 적용 → 15단계 확인 통과. 단계별 시각과 전체 소요 시간을 기록한다.
2. 같은 계획을 다시 적용하면 모든 단계가 `SKIPPED`이고 클러스터 변경이 없다.
3. 중간 단계를 일부러 실패시킨 뒤 재실행하면 실패한 단계부터 이어진다.
4. 렌더러 출력을 바꾼 상태에서 이전 승인으로 적용하면 `REFUSED`가 된다.
5. 에이전트 신원으로 승인 · 적용을 호출하면 거부되고 감사 기록이 남는다.
6. 비밀값이 계획 · 실행 기록 · 로그 · 응답 어디에도 없다(`public_evidence.py check`, 값 검색).
