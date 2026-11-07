# T45 목적지 처음 구축: 정형 스택 → 렌더 → 계획 digest → 적용

설계 8.11의 "목적지 구축" 어댑터를 [ADR-0033](../../docs/adr/0033-destination-bootstrap-executor.md)대로 만든다. 빈 클러스터에서 목적지를 처음 세우는 순서를 맡고, 이미 선 목적지의 설정 모듈 · 원하는 상태 · 고객 클러스터 Argo CD 설치는 T44([ADR-0032](../../docs/adr/0032-destination-iac-api.md), `services/iac`)가 맡는다. 목적지 구축 절차를 정형 단계 스택으로 고정하고, 같은 입력이면 같은 계획과 `plan_digest`가 나오게 한다. 승인은 T10 `approval_kind=infra`(#603)가, 선언형 자원의 적용은 T44가 설치한 목적지 Argo CD(`bravo-iac-gitops`)가 맡는다. T45는 그 Argo에 전용 AppProject와 경로별 Application만 더한다.

**상태: 스택 · 계획 엔진, 적용 실행기, Git 설정 저장소 · kubectl · T10 어댑터, HTTP API, T10 운영자 신원 연결(`serve`)까지. 시험 44개 통과. 독립 리뷰 결함 11건 중 높음 1 · 중간 4 · 낮음 5를 고치고 회귀 시험을 붙였다. kind(Kubernetes v1.36.1) 실측 결과는 [증빙](../../docs/evidence/t45-kind-run-2026-11-07/README.md). 에이전트 신원(T38 워크로드 신원)과 플랫폼 배포는 다음 PR이다.**

| 파일 | 역할 |
|---|---|
| `schemas/stack.schema.json` | 스택 v1. 단계 종류 8개(`probe` · `operator_action` · `helm_release` · `gitops_publish` · `gitops_sync` · `render_apply` · `secret_generate` · `wait`)와 종류별 필드. 셸 명령 필드는 없다 |
| `stack.py` | 스택 검사(스키마, 중복 · 없는 의존 · 순환, publish 없는 sync, 선언하지 않은 파라미터), 병렬 단계(level) 계산, 계획 요청 JSON Schema 생성 |
| `plan.py` | 저장소 렌더러를 고정 인자로 실행하고, chart는 아카이브 해시를 확인한 뒤 `helm template` → 후처리(`finalize-bao` 등)를 거친다. 렌더 결과를 자원 종류로 골라 나눌 수 있다. 단계 digest, 설정 저장소 트리 digest, `plan_digest` |
| `executor.py` | 승인 대조 → 1회 소비 → 단계 그래프 병렬 실행. 단계 종류별 처리, 멱등 `SKIPPED`, 사람 단계 `WAITING_OPERATOR`, 결과 불명 `HOLD`, 재개 |
| `applications.py` | 경로마다 목적지 Argo CD Application 1개(실행이 올린 커밋 SHA 고정, 자동 prune 끔, self-heal 켬)와 스택 AppProject. AppProject는 설정 저장소 하나, 승인된 매니페스트가 만들거나 쓰는 namespace만, 그 매니페스트에 실제로 있는 클러스터 범위 종류만 허용한다(`*` 없음, T44 자원 없음) |
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
uv run bravo-infra-plan check ../../infra/stacks/target-onprem-migration/stack.json   # 목적지 이전 단계
uv run bravo-infra-plan request-schema ../../infra/stacks/target-onprem/stack.json
uv run bravo-infra-plan plan ../../infra/stacks/target-onprem/stack.json \
  --request request.json --repo-root ../.. --charts-dir <chart 아카이브 폴더> \
  --stack-revision <Git SHA> --cluster-uid <UID> --node-uid <UID> --base-commit <Git SHA> \
  --out <새 출력 폴더>
```

chart 아카이브는 스택의 `charts`에 적힌 파일 이름으로 둔다. 해시가 다르면 `chart_hash_mismatch`로 거부한다. `plan`은 클러스터와 Git 원격에 접속하지 않으며, 대상 UID와 기준 커밋은 호출자가 넘긴다(다음 PR의 API는 직접 읽는다).

## 실행 규칙 (ADR-0032 3.4)

- 실행 시작 때 대상 클러스터 UID와 설정 저장소 head를 다시 읽어 계획을 다시 렌더한다. 기대 binding은 계획 기록의 `infra_plan_id` · `requested_by`와 다시 계산한 `plan_digest` 세 필드다. T10 승인의 `plan_digest`와 다르면 `plan_digest_mismatch`, digest는 같지만 다른 계획 · 요청자의 승인이면 `binding_mismatch`로 `REFUSED`이고 아무것도 바꾸지 않는다. 같으면 T10에서 그 binding 전체로 승인을 이 실행 하나로 소비한다(T10이 binding 세 필드 · 만료 · 회수 · `apply_infra` · 원 승인자 권한을 다시 확인). CLI `run`은 `--infra-plan-id`와 `--requested-by`로 승인한 binding을 받는다. 재개도 실행 기록에 저장한 binding으로 다시 소비하고, binding이 없는 옛 실행 기록은 `HOLD`(`run_binding_missing`)다.
- 의존이 끝난 단계는 동시에 시작한다(기본 4개). 실패 · `HOLD` · 사람 대기는 그 단계에 의존하는 단계만 멈춘다.
- `gitops_publish`는 설정 저장소가 실행이 아는 head에서 움직이지 않았을 때만, 그 경로만 담은 커밋 하나를 fast-forward로 push하고(작업 폴더 파일 잠금, 사용자 git 설정 무시), 그 커밋 SHA를 Application에 고정한다. Argo CD가 이미 같은 내용을 따르고 있으면 커밋 · Application 변경 없이 `SKIPPED`.
- `gitops_sync`는 Application이 그 SHA로 `Synced` · `Healthy`일 때만 통과한다. 시간 상한을 넘기면 `HOLD`. 뒤 단계 전에는 Ready일 수 없는 워크로드만 `health`로 받을 상태를 적는다(`openbao-sync`: init · unseal 전 `Progressing`, Ready는 `openbao-init`이 확인).
- `secret_generate`는 Secret이 없을 때만 만든다. 값은 메모리 → `kubectl create` stdin뿐이고, 실행 기록 · 원장에는 남지 않는다(시험으로 확인).
- 사람 단계는 확인 조건을 먼저 관찰한다. 관찰되면 `SKIPPED`. `expect`를 적은 단계는 코드가 관찰한다: 오프라인 이미지는 노드가 보고하는 이미지 이름(`images` · `images_by_arch`), 호스트 디렉터리는 준비한 쪽(첫 부팅 또는 플랫폼의 사이트 호스트)이 노드에 남긴 `bravo.io/host-paths` 주석이 선언한 디렉터리의 digest와 같은지. 이런 단계는 사람 확인만으로 넘어가지 않는다(`confirm_check_failed`). `expect`가 없는 단계만 사람 확인 기록으로 진행한다.
- 파라미터는 `default`를 둘 수 있고, 기본값은 계획 요청에 적혀 digest에 들어간다. 단계는 `when`(닫힌 값 파라미터와 그 값)으로 대상에 따라 빠질 수 있다. 빠진 단계는 계획에 `omitted`로 남고(승인이 그 생략을 덮는다) 렌더하지 않으며, 실행에서는 `OMITTED`(의존 단계에는 끝난 것). 예: `tailnet=none`(같은 클러스터 안 격리 시험)은 Tailscale 줄기를, amd64 노드는 `app-image`를 뺀다.
- 어댑터 오류(`HOLD` `adapter_error`)는 실행 기록의 `error`에 예외 종류와(어댑터의 고정 메시지인 경우) 원인을 남긴다. kubectl 읽기 조회는 API에 닿지 못한 전송 오류(`Unable to connect to the server`, broken pipe 등)일 때만 세 번까지 다시 묻는다. 쓰기는 다시 보내지 않는다.
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

## 목적지가 이전 대상으로서 하는 단계 (`target-onprem-migration`)

`target-onprem`으로 목적지를 세운 뒤, 목적지가 이전 대상이 되기까지의 순서다. 같은 실행기·계획 digest·T10
`approval_kind=infra` 승인으로 돌고, 단계마다 대상 신원을 다시 읽는다. 클러스터에 쓰는 것은 정방향 구독을 **만드는**
`render_apply` 하나뿐이다. 지우기·끄기·덮어쓰기는 없다. 관찰할 수 없는 것은 사람 확인으로 남기고, 시간이 걸리는 것은
`operator_action`의 확인 검사로 기다린다(관찰되면 resume 때 SKIPPED, 아직이면 WAITING).

| 단계 | 종류 | 담당 | 끝나는 조건(코드가 관찰) |
|---|---|---|---|
| `identity` | probe | T30 | 승인된 노드·클러스터 UID, 노드 Ready |
| `node-files` | operator_action | T44 | API 서버가 Metadata 감사 이벤트를 낸다(`apiserver_audit_level_total{level="Metadata"}` > 0). 노드 묶음은 `infra/iac/nodes/<환경>`, 적용기는 `infra/iac/nodes/apply.py` |
| `iac-network` | operator_action | T44 | T44 판정기 `infra/iac/customer_status.py`(#635)가 `bravo-iac-target-dev`를 승인 커밋으로 `SYNC_VERIFIED`. 설치 전·수렴 전이면 기다리고, 판정기가 막으면 HOLD |
| `replication-external-cluster` | probe | T8 | `target-db`가 `t8-source`를 선언했다(kube DNS `source-db-for-replication.target-dev.svc`, verify-full, 자격 Secret 이름). 선언은 `target-onprem`의 database 렌더가 소유하고, 여기서는 확인만 한다 |
| `replication-credentials` | operator_action | T8 | `target-dev/t8-source-replication`(password), `t8-source-ca`(ca.crt)가 있고 CNPG가 읽었다(`status.secretsResourceVersion.externalClusterSecretVersion`). 둘 다 라벨 `cnpg.io/reload=true`가 필요하다. 값은 Git·계획·기록에 없음 |
| `replication-target-ready` | probe | T8 | 구독이 이미 있거나, 게시된 표가 목적지에 있고 비어 있다. 행이 있으면 HOLD |
| `forward-subscription` | render_apply(`create_only`) | T8 | `replication.py subscription --side target`: `t8-source-to-target`이 없으면 만든다. 같으면 SKIPPED, 다르면 HOLD |
| `initial-copy` | operator_action | T8 | 구독 적용 + 표마다 초기 복사 끝(`pg_subscription_rel` 전부 `r`, 주 인스턴스에서 고정 읽기 쿼리). 인증서 이름 불일치·CA 미적재는 HOLD |

복제 판정(볼 대상, 고정 읽기 쿼리, 판정)은 `bravo_infra_plan/replication.py`에 있다. `KubectlCluster`는 객체 읽기와
주 인스턴스의 읽기 전용 쿼리만 한다. 출발지의 역방향 준비와 T10 넘김은 이 스택 밖이다(목적지 UID 승인에 묶이지 않는다).

그다음은 플랫폼 워크플로다: REHEARSE(T31 반영) → GATE(T19 재대조) → AWAIT_APPROVAL(요청자가 아닌 사람) → SHIFT(T17
가중치) → CUTOVER(T12 실행기: 비교·커밋, 정방향 끔·역방향 켬, epoch) → WATCH(T36 SLI + T19 재시험). 문제가 있으면
FAILBACK(T20)이 역방향 복제와 Edge 복원으로 돌린다. 이 스택은 그 판단을 하지 않고, 그 앞까지를 순서와 조건으로 고정한다.

새 RKE2 시험 VM(2026-11-07 20:30–20:42 KST, CNPG 1.30.1, 클러스터 UID 대조)에서 실제 렌더 출력을 이름만 바꾼
namespace에 올려 확인했다. 외부 클러스터를 선언한 `target-db`는 Secret 없이도 healthy였다. 늦게 만든 Secret은
`cnpg.io/reload` 라벨이 있어야 적재됐고, 그 뒤 kube DNS + verify-full로 초기 복사가 끝났다(행 수 일치, TLSv1.3). 판정
함수(`external_cluster`·`credentials`·`target_ready`·`initial_copy`)는 실제 `KubectlCluster`로 모두 True였다. 손으로
`hostaddr`를 패치한 기존 목적지는 `external_cluster`가 False다. 새 렌더로 세운 목적지에서만 통과한다.

## 다음 PR에서 할 것

- 에이전트 워크로드 신원(T38)을 API에 연결(ADR-0033의 "에이전트는 계획 생성 · 조회"), T43 경로의 플랫폼 배포.
- 목적지 Argo CD AppProject의 클러스터 범위 자원 허용을 좁히기(ADR-0032 3.5 후속).
