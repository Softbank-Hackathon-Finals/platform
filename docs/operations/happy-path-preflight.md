# 해피패스 실행 전 점검 (설정 · TLS · 정합성)

이 문서는 해피패스 한 바퀴 직전에 운영자가 확인하는 순서다. 비밀 값은 적지 않고 위치만 적는다.

## 1. 원격 플랫폼 (AWS RKE2)

| 순서 | 할 일 | 확인 |
|---|---|---|
| 1 | `./workon target-check t10-db --route platform-rke2-ssm` | VERIFIED, 클러스터 UID 일치 |
| 2 | Reader 이미지 고정값 갱신과 **설정 v2 전환을 같은 rollout으로** (`operations/product-reader/README.md`) | Reader Ready, 이미지 digest, 설정 sha256 |
| 3 | Argo `initialProposal` 활성(namespace `bravo-initial`·SA·broker CA·승인 정책) | 승인 정책 dry-run 거부/허용 |
| 4 | broker 재설치: `install.py --model-config infra/gitops/environments/development/agent-model.json` + `sandbox_docker` | `bravo-subscription-service` 비활성 상태에서 교체, 설치 소스 SHA |
| 5 | `subscription-runtime` Terraform plan → apply (GitHub trust·controller 역할 제거, 노드 역할 trust) | plan 변경 목록이 위와 같음 |
| 6 | 워크플로 호스트: `workflowHost.runtime`에 ports ConfigMap·Reader v2·state PVC·executor Secret·App Secret | 아래 ports 예시와 일치 |

### 권한 (OperatorAccess)

- 워크플로 호스트는 자기 주체(`workflow-host`)로 T31 반영을 접수한다. 이 주체에 `read`·`apply_change`를 계획별로
  부여한다(`set_permissions`). 원래 승인자는 `approve_change`로 다시 확인되므로 승인과 반영은 분리된다.
- AWAIT_APPROVAL → SHIFT는 사람의 SHIFT 승인(`approve_shift`)이 필요하다. 콘솔에 아직 버튼이 없으면 운영 API
  `/v1/plans/{id}/approvals`(`approval_kind: shift`)로 한다. 실행기 `s3-shift-approval`.
- Job 주체(`b3d65883…`)의 T10 initial grant를 둔 뒤 `run.py fence → admit → start → collect` 순서로 한다.
- relay는 워크플로 호스트 Pod에서만 돈다. 호스트 ConfigMap의 `relay.json`에
  `{"job_principal": "<b3d65883… 전체>", "fence_principals": ["<d26db92a… 전체>"]}`를 둔다. 모델 작업은 Job 주체만,
  fence는 지정한 이전 주체만 가능하고, fence 증명은 Pod 안에서 공개 GitHub로 다시 확인한다.

- SHIFT 직전 재평가(`gate:evaluate`) 권한은 **SHIFT를 승인·실행하는 운영자에게만, 그 계획 범위로** 둔다.
  워크플로 호스트에는 주지 않는다(승인과 반영 주체 분리, 상무님 #546 판단).

### 일괄 롤아웃 중 병합 멈춤 (팀 합의 2)

1. 시작을 해피패스 스레드에 알리고, 자동 큐 등록을 끈다: `gh variable set MERGE_QUEUE_BOT_ENABLED --body false`.
2. 빌드 입력(`services/*`, `infra/charts/*`, 이미지 Dockerfile)을 바꾸는 PR은 큐에 넣지 않는다. 문서·증빙 PR은 그대로 진행한다.
3. 끝나면 `MERGE_QUEUE_BOT_ENABLED`를 원래 값으로 되돌리고 종료를 알린다.
4. 이미지 고정 PR은 합의 1에 따라 develop의 이전 커밋 소스도 병합할 수 있다. 단, 배포 중인 소스보다 새로워야 하고(되돌림 금지),
   develop과의 빌드 입력 차이를 CI 요약에 남긴다(`registration_delivery.py check-promotion`). Reader 이미지는 설정 v2와 같은 배포로 묶는다.

### TLS

- DB: `sslmode=verify-full`, `/ca/ca.crt`(`platform-db-ca`). 호스트·Reader·relay 모두 같은 Secret.
- Executor 읽기: https만, `executor` Secret의 `ca.crt`로 hostname까지 검증. 읽기 토큰만, 명령 토큰 없음.
- broker ↔ Job: 공개 broker CA ConfigMap(`initialProposal.brokerCaPem`), Job 토큰 audience `bravo-subscription-broker`, 600초.
- 세션 VM Docker: 클라이언트 인증서 필수(2376), 서버 인증서는 SSM SecureString.
- GitHub App: 설치 토큰 1시간, 다운로드 리다이렉트에 토큰 미전달.

### 워크플로 호스트 ports 예시 (`/workflow/ports.json`)

```json
{"version": 1,
 "executor": {"url": "https://<executor>", "ca_file": "/executor/ca.crt", "token_file": "/executor/read-token",
              "registered_pair": {"source": {"environment_id": "...", "resource_id": "...", "connection_ref": "..."},
                                  "target": {"environment_id": "...", "resource_id": "...", "connection_ref": "..."}}},
 "propose": {"reader_config": "/product-read/reader.json", "principal": "<T10 initial grant principal>",
             "runtime_source_sha": "<AI 실행이 접수된 runtime (collect 출력)>",
             "selections_dir": "/workflow/selections", "demo_chat_clone": "/workflow-state/demo-chat",
             "github_app": {"app_id": "<bravo-platform-host>", "key_file": "/github-app/private-key.pem",
                            "workflow_sha": "3a1a9135…(demo-chat main, #25 이후 검토값 40자)",
                            "workflow_id": 408240730, "state_file": "/workflow-state/dispatches.json"}}}
```

- `workflow_sha`는 demo-chat #25(run-name) 병합 뒤 main(`3a1a9135…`)으로 고정한다. T7 워크플로가 바뀌면 다시 검토해 갱신한다.
- `runtime_source_sha`는 AI 실행 `collect`가 출력한 값과 같아야 한다(저장 결과와 접수 의도가 결속).
- App 권한은 contents·pull_requests·actions 쓰기 + metadata 읽기, 설치는 demo-chat만.

## 2. AI 제안 (S2 시작)

```sh
uv run --directory services/platform-events python ../../operations/initial-proposal/run.py fence  --from initial.json --legacy-intent <원 intent>
uv run --directory services/platform-events python ../../operations/initial-proposal/run.py start  --from initial.json
uv run --directory services/platform-events python ../../operations/initial-proposal/run.py collect --from initial.json
```

- `fence`는 10:08 원 요청 한 번만 필요하다(#543 조건 불충족이면 거부된다).
- `collect` 출력의 `runtime_source_sha`·후보 발행 digest를 T15(f8)에 전달한다.
- exit 10은 대기다. 같은 명령을 다시 실행해도 Job을 다시 만들지 않는다.

## 3. 로컬 목적지 (Mac mini on-prem RKE2)

1. `demo_reset.py check`(실행기 preflight)로 시작 상태 확인.
2. 후보 이미지 이관: `scripts/export_target_image.py`로 발행 digest를 내보내고 목적지 containerd에 같은 digest의
   `localhost/bravo-demo-chat-candidate` 별칭으로 넣은 뒤 digest를 대조한다(실행기 `s2-candidate-import` 확인).
   레지스트리 pull 자격은 목적지에 두지 않는다(목적지 pull형은 고도화).
3. T31 반영 뒤 `open_after_apply.py`(실행기 `s2-open-writes`).

## 4. 실행기

```sh
python3 scripts/happy_path.py run --plan-id <plan> --record run.jsonl --profile real \
  --platform-context <platform> --kubeconfig <target> --initial-args --from initial.json
```

plan 상태는 Reader Pod의 `bravo_platform_events.plan_state`로 읽는다(읽기 전용). T18·T8·T27·이미지 이관은 담당이
실행한 뒤 `confirm`으로 기록한다.
