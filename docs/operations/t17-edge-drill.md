# T17 승인된 Edge 시험 실행 절차

T17 완료 기준 3개(가중치 반영 시간, 실제 분배, 기준 초과 시 자동 0%)를 실제 Cloudflare LB와 실제 T18 부하로 잰다. 해피패스 SHIFT 전에 할 수 있다. 결정은 [ADR-0039](../adr/0039-approved-edge-drill.md), 코드는 `bravo_edge_control.drill`, 운영 도구는 `operations/edge-drill/drill.py`다.

이 문서는 2026-11-07~08 실측에서 실제로 막힌 곳을 순서에 넣었다. 맨 아래 [알려진 함정](#알려진-함정)을 먼저 읽는다.

## 0. 대상과 역할

| 대상 | 경로 | 확인할 신원 |
|---|---|---|
| 플랫폼 RKE2(승인 호스트 `platform-state/t15-rehearsal-host`, T10 DB) | 운영자 tailnet API 프록시, kubeconfig user는 `{token: unused}` | kube-system UID(인계 문서의 값) |
| 목적지 RKE2(`target-dev`) | 같은 방식의 목적지 프록시 | kube-system UID |
| 부하 서버 `bravo-loadgen` | 운영자 SSO로 SSM | `tag:bravo-loadgen`, 인스턴스 이름 |
| 공개 LB | `bravo-edge` 주소 | 응답 헤더 `X-Bravo-Env`(`aws`/`onprem`) |

- **실행 위치:** 실행은 승인 호스트 Pod 안에서 한다. Edge journal(`/records/edge/journal.db`)은 하나다. 같은 시간에 다른 SHIFT·위임 실행이 이 journal을 쓰지 않게 담당과 시간을 맞춘다. 잠금이 두 번째 실행을 거부하긴 한다.
- **승인:** 사람이 한다. 요청자(`--requested-by`, 명세를 준비한 에이전트나 다른 사람)는 승인자가 될 수 없다. 승인한 사람이 실행해도 된다([ADR-0039 개정](../adr/0039-approved-edge-drill.md#5-개정-2026-11-08-류지환-결정)).

## 1. 한 번 준비

### 1-1. 창 생산자를 시험 계획에 묶는다

T10 창 수신은 생산자 credential 하나를 계획 하나에 묶는다. 시험 계획은 SHIFT 전 상태(COLLECT~AWAIT_APPROVAL)여야 한다.

1. **토큰:** 부하 서버 안에서 토큰을 만들고 root 전용 파일(0600)에 둔다. SSM 출력으로는 **SHA-256만** 돌려받는다. 원문 토큰은 서버 밖으로 나가지 않는다.
2. **identity 교체:** 플랫폼의 `platform-state/t10-window-producer-identity`를 `{token_sha256, identity}`로 바꾼다(교체 전 원본을 비공개로 보관). `identity`는 `{subject_id, tenant_id, plan_id, credential_id, expires_at(8시간 이내)}`다. 그다음 `deploy/t10-window-intake`를 재시작한다.
3. **결속:** 승인 호스트 Pod 안에서 `WindowIntake(store).set_binding(identity, enabled=True, expected_revision=…, administration_ref=…, command_key=…)`을 실행한다.
4. **사전 점검:** 부하 서버에서 `bravo-loadgen preflight … --write-rps 0 --window-url http://bravo-window-intake:30981/v1/observability/windows`. "창 수신: 주소 · 토큰 · 계획 연결 확인"이 나와야 한다.

### 1-2. 목적지가 시험 부하를 감당하는지 본다

프로필 `demo-shift`는 창(10초)마다 요청 1,000건 이상, pool마다 100건 이상이 있어야 판정한다(창당 1,000건이면 초당 100건 이상).

- **측정 기준:** 목적지 앱의 읽기는 목적지 pooler를 거쳐 tailnet 너머 출발지 DB로 간다. 단건은 약 200ms다.
- **기본 구성의 한계:** 기본값(`demo-chat` 1개, 앱 DB 풀 10, pooler `default_pool_size=10`)은 100%에서 초당 140건을 넘기면 p95가 1,500ms를 넘었다.
- **올리고 확인:** 시험 전에 pooler pool을 40, `demo-chat`을 4개로 올린다(원래 값은 기록). 목적지 Pod 안에서 Service로 초당 120건을 12초 보내 p95를 확인한다. 11/8 측정은 214ms였다.
- **되돌리기:** 끝나면 원래 값으로 되돌린다.

### 1-3. 시험 명세 파일

비공개 폴더에 run마다 하나씩 둔다. 승인 대상은 `plan_id`·`run_id`·`profile`·`lb_binding`뿐이다.

```json
{"version": 1, "plan_id": "<SHIFT 전 계획>", "run_id": "t17-drill-normal-YYYYMMDD", "approval_id": "PENDING",
 "profile": <docs/observability/threshold-profiles.json의 demo-shift 그대로>,
 "lb_binding": <승인 호스트 /rehearsal/operator.json의 edge.lb_binding>,
 "journal": "/records/edge/journal.db", "token_file": "/edge/token", "operator_host": "/rehearsal/host.json"}
```

정상 전환과 자동 0% 시험은 `run_id`를 따로 둔다. 한 run이 0%로 끝나면 그 run은 닫힌다. 다시 하려면 새 `run_id`와 새 승인이 필요하다.

## 2. 사람의 승인

```sh
cd services/platform-events
uv run --extra sso-registration python ../../operations/edge-drill/drill.py \
  --kubeconfig <플랫폼> --cluster-uid <UID> approve \
  --requested-by <명세를 준비한 에이전트나 다른 사람의 id> normal.json abort.json
```

- digest는 승인 호스트에 배포된 코드가 계산한다. 그래서 로컬 코드와 배포본이 다르면 여기서 드러난다.
- 권한(`read`·`approve_infra`·`apply_infra`)은 이 도구가 주지 않는다. 관리 경로(`set_permissions`, 관리 근거 필요)로 미리 받는다.
- 요청자는 승인자와 달라야 한다(T10이 자기 승인을 거부한다). 에이전트 id(예: `agent-t17-drill`)도 된다.
- 명세에 `approved_by`가 기록된다. `run`은 `apply_infra`가 있는 등록 운영자의 SSO로 실행된다. 승인자 본인도 된다.
- 각 명세 파일에 `approval_id`가 기록된다. 승인은 2시간 유효하다.

## 3. 정상 전환 (10→50→100→0)

1. `drill.py … run normal.json register`: 승인을 그 run에 한 번 소비한다.
2. **부하 시작:** 부하 서버에서 실행한다. `--run-id`는 명세의 `run_id`와 같아야 `align`이 맞출 수 있다.
   ```sh
   bravo-loadgen measure --base-url https://<Edge> --measurement-path edge --generator loadgen-ec2 \
     --plan-id <계획> --source-env aws --target-env onprem --run-id <run_id> --duration 420 \
     --write-rps 0 --read-rps 120 --fresh-connections --out <out> \
     --window-url http://bravo-window-intake:30981/v1/observability/windows
   ```
   첫 창 2~3개가 T10에 저장되는지 확인한다(`sli_windows`에서 그 계획의 행 수).
3. `drill.py … run normal.json sequence`: 결과는 `RESTORED`(4단계 완료) 또는 `ABORTED`(기준 초과로 자동 0%)다.
   - 중간에 멈추면 공개 응답과 영수증으로 현재 가중치를 확인한다.
   - 0이 아니면 `run normal.json restore`로 되돌린다. 0% 명령의 작업 ID는 `sequence`와 같아서 중복 PATCH가 없다.
4. **결과 정리:** 영수증 JSONL(`<spec>.sequence.jsonl`의 `receipt` 줄)을 부하 서버에 두고 `bravo-loadgen align --out <out> --receipts <파일>`. 단계마다 반영 상한(초), provider 확인(초), pool별 p95와 창별 목적지 비율이 나온다.

## 4. 자동 0% 시험

1. `register`(abort 명세) → 부하 시작(새 `run_id`)
2. 목적지 장애를 넣는다: `python3 infra/envs/target-workloads/demo_fault.py inject --current-replicas <현재 pooler 수>`. 목적지 앱의 DB 요청이 5xx가 된다. 복제는 DB에 직접 붙어 영향이 없다.
3. `sequence` → 10% 확인 뒤 첫 실패 창에서 `SET_WEIGHT_0` → `ABORTED`
4. `demo_fault.py clear`로 pooler를 기록한 수로 되돌린다. `align`으로 0% 반영을 확인한다.

## 5. 끝낸 뒤

- **0% 확인:** 공개 응답 표본이 전부 `aws`인지, 마지막 영수증의 `readback_weights`가 `{source: 100, target: 0}`인지 확인한다.
- **원래 값 복구:** 목적지 `demo-chat` replica와 pooler pool을 1-2에서 기록한 값으로 되돌린다.
- **결속 끄기:** `set_binding(enabled=False, expected_revision=…)`. 부하 서버의 토큰 파일을 지운다.
- **#26 기록:** 시작·끝 시각, run ID, 단계별 반영 상한, 창별 목적지 비율, 자동 0% 결과, 0% 재조회를 남긴다. 운영 식별자(계정·풀·LB ID)는 [공개 증빙 절차](../public-evidence-policy.md)로 가린다.

## 중단과 복원

- `sequence`·`watch`를 **도구 시간 제한(예: 에이전트 Bash 기본 120초) 아래에서 실행하지 않는다.** 정상 전환은 몇 분 걸린다.
- `drill.py run`이 Ctrl-C·SIGTERM을 받거나, 증명 공급이 끊기거나, 비정상 종료로 끝나면 승인 호스트의 그 drill 프로세스를 멈추고 `restore`를 자동으로 실행한다. 잠금이 풀릴 때까지 재시도하고, 마지막 줄에 `restored_to_zero`를 출력한다.
- `restored_to_zero`가 false이면 원격 잔존 프로세스를 확인한다: `kubectl -n platform-state exec <host> -c host -- python3 -c "import os;[print(p, open(f'/proc/{p}/cmdline','rb').read()) for p in os.listdir('/proc') if p.isdigit()]" | grep edge_control.drill`. 끝난 뒤 `drill.py … run <명세> restore`를 다시 실행한다.

## 알려진 함정

| 증상 | 원인 | 할 일 |
|---|---|---|
| preflight "창 수신: 응답 404" | `--window-url`에 경로가 없다 | `…:30981/v1/observability/windows`까지 쓴다 |
| preflight "쓰기 차단: aws 닫힘" | 출발지 앱이 쓰기 차단 상태다 | 읽기만 시험한다(`--write-rps 0`). Edge 기준 3개에는 쓰기가 필요 없다 |
| 10% 확인 뒤 창별 목적지 비율이 0~4% | 실제 LB는 연결마다 pool을 고른다. keep-alive 연결 몇 개로는 분배가 보이지 않는다 | `--fresh-connections`(#810) |
| 100% 단계에서 목적지 p95 초과 | 목적지 기본 구성(앱 1개, 풀 10)의 처리량 한계 | 1-2대로 올린 뒤 시험하고 되돌린다 |
| `sequence`가 10% 뒤 바로 끝남, `edge_drill_unavailable` | 이미지의 T36이 오래되거나(#773) 계약 스키마가 빠짐(#802) | Pod 안에서 `from bravo_observability.sli import next_prior_unknown`과 `bravo_observability.contract.schema()`가 되는지 본다. 안 되면 이미지를 다시 빌드한다 |
| `operator_proof_unavailable` | 증명(60초)이 끊겼다 | `drill.py run`이 20초마다 보낸다. 운영자 SSO가 만료되면 다시 로그인한다 |
| `restore`가 `shift_runtime_already_running` | 멈춘 drill이 아직 journal 잠금을 쥐고 있다 | `drill.py`가 재시도한다. 계속되면 위 잔존 프로세스를 확인한다 |
| `origin_baseline_required` | journal의 마지막 영수증이 확인된 0%가 아니다 | 그 run을 `restore`로 0%까지 확인한 뒤 새 run을 쓴다 |
| `drill_approval_unavailable` | 승인·소비·만료·계획 version 중 하나가 바뀌었다 | 계획이 움직였으면(새 proposal 등) 그 version으로 다시 승인한다 |
| bravo-gitops Helm 반영 시 다른 서비스 설정이 되돌아감 | 예전 runtime values 파일이 live와 다르다 | live `helm get values`에서 시작하거나 `--reuse-values`로만 바꾼다. Application은 develop을 따라가므로 이미지 pin은 PR 병합으로만 바꾼다 |
