# T45 목적지 처음 구축: 정형 스택 → 렌더 → 계획 digest → 적용

설계 8.11의 "목적지 구축" 어댑터를 [ADR-0033](../../docs/adr/0033-destination-bootstrap-executor.md)대로 만든다. 빈 클러스터에서 목적지를 처음 세우는 순서를 맡고, 이미 선 목적지의 설정 모듈 · 원하는 상태 · 고객 클러스터 Argo CD 설치는 T44([ADR-0032](../../docs/adr/0032-destination-iac-api.md), `services/iac`)가 맡는다. 목적지 구축 절차를 정형 단계 스택으로 고정하고, 같은 입력이면 같은 계획과 `plan_digest`가 나오게 한다. 승인은 T10 `approval_kind=infra`(#603)가, 선언형 자원의 적용은 T44가 설치한 목적지 Argo CD(`bravo-iac-gitops`)가 맡는다. T45는 그 Argo에 전용 AppProject와 경로별 Application만 더한다.

**상태: 스택 · 계획 엔진, 적용 실행기, Git 설정 저장소 · kubectl · T10 어댑터, HTTP API, T10 운영자 신원 연결(`serve`)까지. 시험 44개 통과. 독립 리뷰 결함 11건 중 높음 1 · 중간 4 · 낮음 5를 고치고 회귀 시험을 붙였다. kind(Kubernetes v1.36.1) 실측 결과는 [증빙](../../docs/evidence/t45-kind-run-2026-11-07/README.md). 에이전트 신원(T38 워크로드 신원)과 플랫폼 배포는 다음 PR이다.**

| 파일 | 역할 |
|---|---|
| `schemas/stack.schema.json` | 스택 v1. 단계 종류 8개(`probe` · `operator_action` · `helm_release` · `gitops_publish` · `gitops_sync` · `render_apply` · `secret_generate` · `wait`)와 종류별 필드. 셸 명령 필드는 없다 |
| `stack.py` | 스택 검사(스키마, 중복 · 없는 의존 · 순환, publish 없는 sync, 선언하지 않은 파라미터), 병렬 단계(level) 계산, 계획 요청 JSON Schema 생성 |
| `plan.py` | 저장소 렌더러를 고정 인자로 실행하고, chart는 아카이브 해시를 확인한 뒤 `helm template` → 후처리(`finalize-bao` 등)를 거친다. 렌더 결과를 자원 종류로 골라 나눌 수 있다. 단계 digest, 설정 저장소 트리 digest, `plan_digest` |
| `executor.py` | 승인 대조 → 1회 소비 → 단계 그래프 병렬 실행. 단계 종류별 처리, 멱등 `SKIPPED`, 사람 단계 `WAITING_OPERATOR`, 결과 불명 `HOLD`, 재개 |
| `applications.py` | 경로마다 목적지 Argo CD Application 1개(실행이 올린 커밋 SHA 고정, 자동 prune 끔, self-heal 켬)와 스택 AppProject(설정 저장소 URL로 제한) |
| `adapters.py` | `FileRunStore` · `FilePlanStore`, `GitConfigRepo`(fast-forward push만, 강제 push 없음), `KubectlCluster`(server-side diff/apply, Secret은 stdin으로 create만, helm 이력의 digest), `T10Approvals`, `ClusterChecks` |
| `api.py` | `POST /v1/infra/plans` · `GET .../plans/{id}` · `GET /v1/infra/request-schema` · `POST .../plans/{id}/runs` · `GET /v1/infra/runs/{id}` · `POST .../runs/{id}/steps/{step}/confirm`. 실행 · 확인은 사람만(에이전트 신원 403) |
| `terraform.py` | `terraform` 단계: 코드 · 모듈 digest, 저장소 밖 입력 digest, 출력 경로 연결, terraform CLI(단계별 데이터 폴더, 읽기 전용 lock), 계획의 변경 요약과 삭제 · 교체 판별 |
| `serve.py` | API 호스트. 사람은 T10 승인 호스트와 같은 `operator_verifier`(IAM Identity Center 증명 또는 고정 OIDC 토큰)로 확인하고, 같은 토큰을 T10에 넘겨 그 사람의 `apply_infra`를 다시 확인받는다. 에이전트 신원은 아직 받지 않는다 |
| `cli.py` | `bravo-infra-plan check / request-schema / plan / run / confirm / status` |
| [`infra/stacks/target-onprem/stack.json`](../../infra/stacks/target-onprem/stack.json) | 첫 스택. T30 · T9 README 절차를 33단계 15레벨로 옮겼다. 목적지 Argo CD는 설치하지 않고 T44 설치를 확인하는 사람 단계로 둔다. 렌더러 코드는 바꾸지 않았다 |

## 계획이 담는 것

`plan_digest` = 정규화한 계획 요청 + 스택 digest + `stack_revision`(Git SHA) + 대상 클러스터 · 노드 UID + 설정 저장소 기준 커밋 + 단계별 digest. 하나라도 바뀌면 digest가 바뀐다(시험으로 확인).

- `gitops_publish`: 설정 저장소 `<path>/<name>.json`에 올릴 매니페스트. 트리 digest에 들어간다.
- `render_apply`: 일회성 자원(마이그레이션 Job). 설정 저장소에 두지 않는다.
- `helm_release`: chart 디렉터리(`Chart.lock` 포함, 내려받은 `charts/` 제외)와 values의 digest.
- `secret_generate`: Secret 이름 · 타입 · 키 이름과 생성 방식(`literal` · `random` · `builtin`)만. 값은 실행기가 메모리에서 만든다.
- 그 밖의 단계: 단계 정의 자체의 digest.

## 사용

```sh
uv run bravo-infra-plan check ../../infra/stacks/target-onprem/stack.json
uv run bravo-infra-plan request-schema ../../infra/stacks/target-onprem/stack.json
uv run bravo-infra-plan plan ../../infra/stacks/target-onprem/stack.json \
  --request request.json --repo-root ../.. --charts-dir <chart 아카이브 폴더> \
  --stack-revision <Git SHA> --cluster-uid <UID> --node-uid <UID> --base-commit <Git SHA> \
  --out <새 출력 폴더>
```

chart 아카이브는 스택의 `charts`에 적힌 파일 이름으로 둔다. 해시가 다르면 `chart_hash_mismatch`로 거부한다. `plan`은 클러스터와 Git 원격에 접속하지 않으며, 대상 UID와 기준 커밋은 호출자가 넘긴다(다음 PR의 API는 직접 읽는다).

## 실행 규칙 (ADR-0032 3.4)

- 실행 시작 때 대상 클러스터 UID와 설정 저장소 head를 다시 읽어 계획을 다시 렌더한다. T10 승인의 `plan_digest`와 다르면 `REFUSED`이고 아무것도 바꾸지 않는다. 같으면 T10에서 승인을 이 실행 하나로 소비한다(T10이 digest · 만료 · 회수 · `apply_infra` · 원 승인자 권한을 다시 확인).
- 의존이 끝난 단계는 동시에 시작한다(기본 4개). 실패 · `HOLD` · 사람 대기는 그 단계에 의존하는 단계만 멈춘다.
- `gitops_publish`는 설정 저장소가 실행이 아는 head에서 움직이지 않았을 때만, 그 경로만 담은 커밋 하나를 fast-forward로 push하고(작업 폴더 파일 잠금, 사용자 git 설정 무시), 그 커밋 SHA를 Application에 고정한다. Argo CD가 이미 같은 내용을 따르고 있으면 커밋 · Application 변경 없이 `SKIPPED`.
- `gitops_sync`는 Application이 그 SHA로 `Synced` · `Healthy`일 때만 통과한다. 시간 상한을 넘기면 `HOLD`.
- `secret_generate`는 Secret이 없을 때만 만든다. 값은 메모리 → `kubectl create` stdin뿐이고, 실행 기록 · 원장에는 남지 않는다(시험으로 확인).
- 사람 단계는 확인 조건을 먼저 관찰한다. 관찰되면 `SKIPPED`, 코드가 관찰할 수 없는 것(호스트 디렉터리, 오프라인 이미지 반입)은 사람 확인 기록이 있어야 진행한다.
- 실행은 시작 때 저장한 스택만 실행한다(그 digest가 `plan_digest` 안에 있다). 승인 뒤 저장소의 stack.json을 고쳐도 진행 중인 실행에는 영향이 없다.
- 모든 단계는 대상 클러스터 UID · 노드 UID를 다시 읽고 시작한다. kube context는 시작 때 고정한다. 다르면 `HOLD`(`target_identity_changed`).
- 재개(`confirm` 포함)마다 T10에서 승인 회수 여부를 읽고, 호출자의 토큰으로 같은 실행의 소비를 다시 요청해 그 사람의 `apply_infra`를 확인받는다.
- 실행 기록은 단계별 시작 · 종료 시각을 남긴다. 프로세스가 중간에 죽어 `RUNNING`으로 남은 단계는 재개 때 다시 관찰한다. 재개 전에 설정 저장소가 다른 사람 커밋으로 움직였으면 `HOLD`(`config_repo_moved`).

## Terraform 단계 (AWS 스택 일괄 적용)

[`infra/stacks/aws-platform/stack.json`](../../infra/stacks/aws-platform/stack.json)은 `infra/envs`의 AWS 스택 13개를 의존 순서(3레벨)로 묶는다. 스택 코드는 바꾸지 않는다.

| 레벨 | 스택 |
|---|---|
| 1 | `aws-foundation`, `cost-aws`, `platform-audit` |
| 2 | `source-eks`, `platform-aws` (foundation 출력의 VPC · 서브넷 · 라우팅 테이블을 받는다) |
| 3 | `source-workloads` · `source-backup` · `human-eks-access`(EKS `cluster_name`), `platform-data`(EC2 `instance_id` · VPC), `private-registry`, `executor-registry`, `subscription-runtime`, `sandbox-session` |

- **승인이 묶는 것**: 스택 디렉터리와 그 디렉터리가 부르는 로컬 모듈의 코드 digest, `.terraform.lock.hcl`, 연결 정의, 그리고 저장소 밖 `private_dir`의 `<단계>.backend.hcl` · `<단계>.tfvars.json` **digest**. 계정 · 자원 ID와 backend 값은 Git과 계획 문서에 들어가지 않는다.
- **변경 집합도 승인에 묶는다**: 계획을 만들 때 terraform 단계마다 `plan -lock=false`로 바뀔 주소와 actions(값 없이)를 뽑아 그 digest와 개수를 계획에 넣는다. 앞 스택이 아직 적용되지 않아 출력이 없으면 `PENDING_UPSTREAM`이고, 실행은 그 단계에서 `HOLD`(`terraform_change_set_unapproved`)한 뒤 새 계획 · 승인으로 이어간다(빈 계정의 첫 구축은 레벨마다 승인). 실행 때 다시 뜬 변경 집합이 승인한 것과 다르면 `HOLD`(`terraform_change_set_changed`).
- **실행**: 단계마다 승인 때의 입력 digest를 다시 대조(다르면 `HOLD`) → 단계 전용 `TF_DATA_DIR`에서 `init -lockfile=readonly` → 앞 스택 출력을 상태에서 읽어 메모리로 연결 → `plan -out` → 변경이 없으면 `SKIPPED` → 삭제 · 교체 · `forget`(관리에서 빼기)이 하나라도 있으면 `HOLD`(`terraform_plan_destroys`, 사람이 판단) → 아니면 **그 계획 파일만** `apply`. 출력값은 실행 기록에 저장하지 않는다(여러 출력이 `sensitive`).
- **대상 신원**: 클러스터 대신 STS의 AWS 계정. CLI는 `--aws`로 실행한다.
- **범위 주의**: `platform-aws` · `platform-data` · `subscription-runtime` · `sandbox-session`은 이 실행기가 돌고 있는 플랫폼 자신을 바꿀 수 있다. 첫 실측은 읽기 전용 `verify`만 하고, 적용 범위는 담당(류지환)과 정한 뒤 연다.
- backend 설정 값은 프로세스 인자로 넘기지 않는다(개인 backend 파일과 0600 임시 파일).
- **넣지 않은 것**: `ci-validation-aws`(로컬 backend, 일회성), 아직 적용하지 않은 `loadgen-aws` · `attachment-copy-reader`.
- **검증**: 실제 terraform 바이너리로 로컬 backend 시험 스택 두 개(출력 연결, 재실행 `SKIPPED`, 교체 `HOLD`, 승인 뒤 입력 변경 거부). 실제 AWS 계정에는 실행하지 않았다.

## 다음 PR에서 할 것

- 원장(`ledger`)을 T24 감사 로그 싱크에 연결한다. 지금은 JSONL 파일이다.
- 에이전트 워크로드 신원(T38)을 API에 연결(ADR-0033의 "에이전트는 계획 생성 · 조회"), T43 경로의 플랫폼 배포.
- 목적지 Argo CD AppProject의 클러스터 범위 자원 허용을 좁히기(ADR-0032 3.5 후속).
