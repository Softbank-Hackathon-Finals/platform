# 해피패스 실행 전 점검 (설정 · TLS · 정합성)

이 문서는 해피패스 한 바퀴 직전에 운영자가 확인하는 순서다. 비밀 값은 적지 않고 위치만 적는다.

## 1. 원격 플랫폼 (AWS RKE2)

| 순서 | 할 일 | 확인 |
|---|---|---|
| 1 | `./workon target-check t10-db --route platform-rke2-ssm` | VERIFIED, 클러스터 UID 일치 |
| 2 | Reader 이미지 고정값과 설정 v2 (순서 무관: Reader는 v1에서도 뜨고 최초 경로만 닫는다. `operations/rollout/run.py`) | Reader Ready, 이미지 digest, 설정 sha256 |
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

- SHIFT 직전 재평가 권한(`gate:evaluate`·`gate:consume`, 해당 gate context)은 **SHIFT를 승인·실행하는 운영자
  (`approve_shift`·`execute_shift` 보유)에게만, 그 계획 범위로** 둔다(상무님 #532).
  워크플로 호스트에는 주지 않는다(승인과 반영 주체 분리, 상무님 #546 판단).

### 일괄 롤아웃 중 병합 멈춤 (팀 합의 2)

1. 시작을 해피패스 스레드에 알리고, 자동 큐 등록을 끈다: `gh variable set MERGE_QUEUE_BOT_ENABLED --body false`.
2. 빌드 입력(`services/*`, `infra/charts/*`, 이미지 Dockerfile)을 바꾸는 PR은 큐에 넣지 않는다. 문서·증빙 PR은 그대로 진행한다.
3. 끝나면 `MERGE_QUEUE_BOT_ENABLED`를 원래 값으로 되돌리고 종료를 알린다.
4. 이미지 고정 PR의 최신성 검사 완화(합의 1)는 롤아웃 뒤 별도 PR로 한다. 롤아웃 중에는 기존 검사 그대로다.
   Reader는 v1·v2 설정을 모두 읽으므로 이미지와 설정 v2의 반영 순서는 무관하다.

### relay 접근 (sangmu1126 76ee08d)

relay는 호스트 Pod의 DB 자격으로 T10에 쓴다. 그래서 platform-state의 `pods/exec` 보유자를 롤아웃 직전에 확인해 운영자
한 명인지 기록한다. `kubectl get roles,clusterroles,rolebindings,clusterrolebindings -A -o json`에서
platform-state에 `pods/exec`(또는 `*`) create를 주는 바인딩의 주체를 뽑아 실행 기록에 남긴다.

### 네트워크 (워크플로 호스트)

platform-state는 기본 차단이다. DB는 기존 `bravo.dev/db-client=runtime` 정책으로 열린다. 호스트를 켜기 전에
`infra/envs/workflow-host/network.py --github-meta meta.json`(공개 `https://api.github.com/meta`)의 정책 두 개를
server dry-run 뒤 적용한다: 호스트 egress(DNS, Executor `bravo-platform/cutover-executor:8080`, GitHub 443 IPv4)와
Executor ingress(호스트에서만).

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
- 고정 선택(`selections_dir`)은 폴더가 없어도 된다. ConfigMap 마운트는 하위 폴더를 만들 수 없으므로, 같은 ConfigMap에
  `selections__<request_id>.json` 키로 두면 `/workflow/selections/<request_id>.json`과 같게 읽는다.
- 서버가 시작하지 못하면 종료 메시지에 원인 코드(예외 이름·빠진 모듈·식별자 코드)가 붙는다. 이미지 빌드는
  진입점의 import 폐포(`scripts/image_import_closure.py`)가 빠진 모듈 없이 닫혀야 통과한다.
- `runtime_source_sha`는 Reader 이미지 고정값(`product-reader-image.json`)의 `sourceRevision`이다. `run.py`가 같은
  값으로 접수하고 `collect`도 같은 값을 출력한다(저장 결과와 접수 의도가 결속). 그래서 Reader 고정값이 바뀌면 다시 맞춘다.
- App 권한은 contents·pull_requests·actions 쓰기 + metadata 읽기, 설치는 demo-chat만.

## 2. AI 제안 (S2 시작)

```sh
uv run --directory services/platform-events python ../../operations/initial-proposal/run.py fence  --from initial.json --legacy-intent <원 intent>
uv run --directory services/platform-events python ../../operations/initial-proposal/run.py admit  --from initial.json --tenant-id <tenant> --task-id <task> --expires-at <시각>
uv run --directory services/platform-events python ../../operations/initial-proposal/run.py start  --from initial.json
uv run --directory services/platform-events python ../../operations/initial-proposal/run.py collect --from initial.json
```

- `fence`는 10:08 원 요청 한 번만 필요하다(#543 조건 불충족이면 거부된다). `admit`은 fence 뒤 같은 입력을 Job 주체로
  다시 접수하고 `--ref` 파일을 새 입력으로 바꾼다. Job 주체의 initial grant가 먼저 있어야 한다.
- `collect` 출력의 `runtime_source_sha`·후보 발행 digest를 T15(f8)에 전달한다.
- exit 10은 대기다. 같은 명령을 다시 실행해도 Job을 다시 만들지 않는다.

## 3. 로컬 목적지 (Mac mini on-prem RKE2)

1. `demo_reset.py check`(실행기 preflight)로 시작 상태 확인.
2. 후보 이미지 이관: `scripts/export_target_image.py`로 발행 digest를 내보내고 목적지 containerd에 같은 digest의
   `localhost/bravo-demo-chat-candidate` 별칭으로 넣은 뒤 digest를 대조한다(실행기 `s2-candidate-import` 확인).
   레지스트리 pull 자격은 목적지에 두지 않는다(목적지 pull형은 고도화).
3. T31 반영 뒤 `open_after_apply.py`(실행기 `s2-open-writes`).
4. 첨부 복사(`s3-attachment-copy`, T8 담당) **직전**에 출발지 첨부 업로드를 멈춘다. 복사는 시작 뒤에 들어온 업로드를 알 수
   없기 때문이다. 출발지 앱 이미지(`8d82a92`)의 `ATTACHMENT_UPLOAD_ENABLED=false`로 하고, 출발지 앱 Pod가 한 번 다시 뜬다.
   멈춘 시각과 Pod 재기동을 실행 기록에 남기고, 컷오버가 끝날 때까지 되돌리지 않는다(실패 복귀 시에만 `true`로 복원).
   첨부 복사의 `plan_id`·`epoch`는 컷오버 요청 값을 그대로 쓴다(T10 담당이 그 단계 직전에 전달).
5. WATCH의 T18 부하 창은 전용 서버 `bravo-loadgen`에서만 보낸다(노트북 송신은 차단). 해피패스 전에 켜고 표시 파일과
   tailnet 태그를 확인한다(T18 담당).

## 4. 실행기

```sh
python3 scripts/happy_path.py run --plan-id <plan> --record run.jsonl --profile real \
  --platform-context <platform> --kubeconfig <target> --initial-args --from initial.json   # --initial-args는 맨 마지막
```

plan 상태는 워크플로 호스트 Pod(`deploy/t10-workflow-host`, 컨테이너 `host`)의 `bravo_platform_events.plan_state`로 읽는다(읽기 전용). T18·T8·T27·이미지 이관은 담당이
실행한 뒤 `confirm`으로 기록한다.

## 5. 운영자 승인 API (리허설 호스트 Pod)

변경·SHIFT·컷오버·되돌리기 승인과 T15 리허설은 한 Pod(`platform-state/t15-rehearsal-host`, `approval_server` + `operator_ports`)에서 실행한다.

- 들어오는 경로: Service는 ClusterIP 8443뿐이다. 공개 ingress는 없다. 승인자는 자기 SSO로 SSM 터널을 연 뒤 `kubectl -n platform-state port-forward svc/t15-rehearsal-host 8443:8443`로 접속한다. 요청마다 운영자 audience(`bravo-platform-operator`)로 서명한 AWS SSO 증명을 Bearer로 보낸다.
- 운영자 신원: `operator_verifier`에 사람별 고정 신원 목록을 둔다. 승인자와 실행자는 다른 사람이어야 한다. `authenticated_at`은 증명 발급 시각이며, SSO(MFA) 로그인 시각보다 늦을 수 있다.
- SHIFT CLI: 같은 Pod에서 `kubectl exec ... python -m bravo_edge_control.run`으로 실행하고, journal은 `/records/edge/journal.db` 하나를 쓴다. FailbackEdge도 같은 journal을 쓴다.

### 알려진 위험: 실행 권한이 한 Pod에 모임 (해피패스 한정)

이 Pod에는 다음이 함께 있다. 리허설 경로 하나가 뚫리면 컷오버 제출과 트래픽 전환까지 닿는다.

- 후보 코드를 리허설 namespace에 반영하는 경로와 exec relay
- DB 자격, catalog 키
- Executor command-token(컷오버 제출)
- Cloudflare 토큰(트래픽 가중치)

해피패스 동안 지킬 것:

1. Cloudflare 토큰은 그 LB 하나의 pool 가중치 변경만 가능한 범위로 발급한다. 데모 직후 폐기한다.
2. command-token은 E2E 직전에 발급하고, E2E가 끝나면 Secret을 지우고 Executor 쪽 토큰을 교체한다.
3. egress는 DNS, tailnet 프록시, Executor 8080, STS 고정 주소, Cloudflare API 대역 443만 연다(`infra/envs/rehearsal-access/rehearsal-host-network.yaml`).

해피패스 뒤: 승인 API와 Edge 실행을 같은 이미지의 별도 Deployment·SA·NetworkPolicy로 분리한다. command-token과 Cloudflare 토큰은 그 Pod에만 둔다. 리허설 호스트에는 남기지 않는다.
