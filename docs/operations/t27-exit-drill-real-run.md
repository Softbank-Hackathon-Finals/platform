# T27 Exit Drill 실제 실행 안내

**앱이 `target-dev`에 배포되면 잔존 의존성 지도부터 뽑고, 컷오버 뒤에 차단 시험으로 넘어간다.** 차단은 앱과 풀러 Pod의 송신만 좁히고 DB Pod를 고르지 않으므로 복제 채널을 끊지 않는다. 지금 앱은 호환 구성([ADR-0017](../adr/0017-target-app-secrets-manager-compat.md))이라 **PASS가 나오지 않는 것이 맞다.**

이 순서는 임시 k3s에서 그대로 돌려 봤다([증거](../evidence/t27-real-run-prep-2026-11-06/README.md)). 실제 RKE2 · demo-chat · CNPG · Tailscale · AWS에서는 아직 돌리지 않았다.

| 단계 | 언제 | 바꾸는 것 | 나오는 것 | 시간 |
|---|---|---|---|---|
| [1. 지도](#1-지도-뽑기) | 앱 배포 직후 | 대조군 Pod 하나 | 판정 `NOT_RUN`인 지도 | 5분 |
| [2. 차단 시험](#2-차단-시험) | 컷오버 뒤 | 차단 정책, 앱 · 풀러 재시작 | 판정 보고서와 그 판정을 반영한 지도 | 15분 안쪽 |
| [되돌리기](#되돌리기) | 끝나거나 멈출 때 | 정책과 대조군 삭제 | 차단 전 상태 | 1분 |

호환 구성에서 2단계를 돌리면 `eligibility:app`이 `NOT_EXIT_EVIDENCE`로 UNKNOWN이고 종료 코드는 3이다. 지도는 그대로 나온다. PASS를 보려면 호환 구성을 없앤 T31 이미지가 필요하다.

**이 비자격 판정은 PR #210의 코드에 있다.** 그 코드가 없으면 호환 구성도 PASS로 나온다(임시 k3s에서 실제로 그렇게 나왔다). `services/exit-drill/src/bravo_exit_drill/verdict.py`에 `_check_eligibility`가 없으면 2단계를 돌리지 않는다.

**컷오버 전에는 2단계를 하지 않는다.** 차단 대상에 풀러 → 출발지 경로가 있어서, 앱의 DB 경로가 시험 동안 끊기고 `NEW_CONNECTION_FAILED`로 FAIL이 나온다. 앱 탓이 아니므로 이 결과는 쓸 데가 없다. 지도만 필요하면 1단계로 충분하다.

## 무엇을 막고 무엇을 남기나

**앱과 풀러 Pod의 송신을 선언한 내부 경로로만 좁힌다.** 이유는 [ADR-0014](../adr/0014-exit-drill-allowlist.md)에 있다. 수신과 다른 Pod는 건드리지 않는다.

| Pod | 선택자 | 정책 | 결과 |
|---|---|---|---|
| 앱 | `app=demo-chat` | 송신 제한 | DNS · 풀러 · SeaweedFS만 나간다. AWS API와 tailnet은 막힌다 |
| 풀러 | `app=target-pgbouncer` | 송신 제한 | DNS · 목적지 DB만 나간다. 출발지 DB 프록시는 막힌다 |
| 목적지 DB | `cnpg.io/cluster=target-db` | **고르지 않음** | 복제 정방향 · 역방향이 그대로다 |
| SeaweedFS, 대조군 | | 고르지 않음 | 그대로다 |
| `tailscale-system`의 프록시 | | 고르지 않음 | 그대로다 |

복제를 끊을 입력은 `render`가 만들지 않고 거부한다. 앱 선택자가 DB Pod를 함께 고르면 `REPLICATION_OWNER_SELECTED`, 차단 경로가 복제 경로와 같으면 `BLOCK_HITS_REPLICATION`, 기존 정책이 이미 복제를 막고 있으면 `REPLICATION_BLOCKED`다.

이미 맺은 연결은 정책을 넣어도 끊기지 않는다(k3s 실측). 그래서 앱과 풀러를 재시작해 연결을 새로 맺는다. 이 시험 동안 앱은 재시작하므로, Edge 경유 측정(T17 · T18)과 겹치지 않게 한다.

## 시작 전에

- **대상 확인.** `kubectl config current-context`와 노드 이름이 개발 VM인지 확인한다. 도구는 현재 컨텍스트를 그대로 쓴다.
- **저장소 밖 폴더.** 결과에는 Pod 이름 · UID · 컨테이너 ID가, 인벤토리에는 ARN과 계정 번호가 들어간다. 저장소 안에 두지 않는다. 공개할 때는 [공개 증빙 절차](../public-evidence-policy.md)를 따른다.

```sh
export OUTPUT_DIR=<저장소 밖 폴더>
cd services/exit-drill && uv sync --locked
```

| 입력 | 어디서 | 없으면 |
|---|---|---|
| T16 인벤토리 | 아래 명령. 실제 demo-chat과 실제 내보내기로 만든다 | 시작하지 못한다 |
| 대조군 이미지 | 장비에 이미 있고 Python이 든 이미지. 노드 아키텍처(지금 arm64)의 앱 이미지를 쓴다. `python3 ../../infra/envs/target-workloads/application.py --arch arm64 --print-image` | `render`가 `SELECTOR_MATCHES_NOTHING`으로 멈춘다 |
| 컷오버 확인 | T12 실행 기록의 상태가 `DONE`인지 사람이 본다. 2단계에서만 쓴다 | `CUTOVER_NOT_CONFIRMED`로 UNKNOWN |
| 목적지 쓰기 | 2단계 측정 중 목적지 DB에 쓰기가 있어야 한다. 아래 4번 | `REPLICATION_IDLE`로 UNKNOWN |
| 권한 | Pod 읽기 · 생성 · 삭제 · exec, Deployment 재시작, NetworkPolicy 적용 · 삭제 | 해당 단계에서 멈춘다 |

T16 인벤토리는 이렇게 만든다. 자세한 내용은 [T16 안내](../../services/inventory/README.md)에 있다. `<>` 값은 실제 환경에서 읽어 넣는다. `onprem-demo`는 `profiles/onprem-rke2.json`의 `env_id`다. demo-chat 체크아웃은 목적지 이미지를 만든 커밋(`infra/envs/target-workloads/app-image.json`의 `source_sha`)으로 맞춘다. demo-chat `main`의 끝이 아니다.

```sh
# 목적지 내보내기. 출발지 내보내기(deployments · serviceaccounts · clusters · aws.json)는 T7 수집 역할이 만든다.
mkdir -p "$OUTPUT_DIR/exports/target"
kubectl get deployments -A -o json > "$OUTPUT_DIR/exports/target/deployments.json"
kubectl get crds -o json > "$OUTPUT_DIR/exports/target/crds.json"

(cd ../inventory && uv sync --locked && uv run --locked bravo-inventory scan \
  --app-id demo-chat --repo <demo-chat 체크아웃> --app-path app \
  --source-env <출발지 환경 이름> --namespace <출발지 앱 네임스페이스> \
  --exports <출발지 내보내기 폴더> --profile profiles/onprem-rke2.json \
  --target-exports "onprem-demo=$OUTPUT_DIR/exports/target" \
  --output "$OUTPUT_DIR/inventory.json")
```

**출발지 내보내기 네 파일이 한 폴더에 다 있어야 한다.** T16은 Deployment → ServiceAccount의 IRSA 주석으로 IAM 역할을 찾고, 그 역할로 `aws.json`을 읽는다. ServiceAccount가 빠지면 `aws.json`을 읽지 않아 배포에만 있는 항목이 인벤토리에서 빠진다(2026-11-06 실제 입력에서는 KMS). 못 읽은 것은 `deploy.notes`에 남고 지도 맨 위 **인벤토리 추적 메모**에 나온다. 빠진 파일을 지어내 채우지 않는다.

## 1. 지도 뽑기

**차단 없이 끝난다.** 대조군 Pod를 띄워 계획을 만들고 지도를 낸다. 대조군은 비밀과 계정이 없고 잠만 자며 4시간 뒤 스스로 끝난다.

```sh
uv run bravo-exit-drill control-pod --spec profiles/target-dev.json \
  --image "$(python3 ../../infra/envs/target-workloads/application.py --arch arm64 --print-image)" > "$OUTPUT_DIR/control-pod.json"
kubectl apply --dry-run=server -f "$OUTPUT_DIR/control-pod.json"
kubectl apply -f "$OUTPUT_DIR/control-pod.json"
kubectl -n target-dev wait --for=condition=Ready pod/exit-drill-control --timeout=5m

kubectl -n target-dev get pods,networkpolicies -o json > "$OUTPUT_DIR/snapshot.json"
uv run bravo-exit-drill render --spec profiles/target-dev.json --inventory "$OUTPUT_DIR/inventory.json" \
  --candidate onprem-demo --snapshot "$OUTPUT_DIR/snapshot.json" > "$OUTPUT_DIR/plan.json"
uv run bravo-exit-drill map --inventory "$OUTPUT_DIR/inventory.json" --candidate onprem-demo \
  --plan "$OUTPUT_DIR/plan.json" --format markdown > "$OUTPUT_DIR/residual-map.md"
```

`render`가 거부하면 `plan.json`에 `refused`와 이유가 들어가고 종료 코드는 1이다. 고칠 곳을 찾을 뿐 값을 지어내지 않는다.

2026-11-06 실제 `target-dev` 스냅샷을 읽기만 해 `render`에 넣었을 때(대조군을 띄우기 전) 거부는 `SELECTOR_MATCHES_NOTHING`(control) 하나였다. 검사는 위반을 모아 돌려주므로, 대조군만 띄우면 나머지 검사는 그때 상태로 통과한다는 뜻이다. 명세의 tailnet 프록시 레이블 셋(`bravo-target-pooler` · `bravo-target-replication` · `target-db-tailnet`)도 `tailscale-system`의 실제 Pod에 있었다.

**Edge 터널(`bravo-edge-tunnel`, T17의 `cloudflared`)은 명세의 `edge` 역할이다.** 같은 날 04:01에 올라와 10시대 실제 실행이 `UNCLASSIFIED_POD`로 거부됐고, 이제 차단 정책이 고르지 않는 "들어오는 경로"로 분류돼 지도의 cloudflare 항목에 이어 보인다.

| 코드 | 뜻 | 할 일 |
|---|---|---|
| `SELECTOR_MATCHES_NOTHING` | 그 Pod가 안 떠 있다 | 앱 · 풀러 · DB · SeaweedFS 배포와 레이블을 확인한다 |
| `CONTAINER_NOT_IN_POD` | 명세의 컨테이너 이름이 Pod에 없다 | 실제 이름을 확인해 명세를 고친다 |
| `UNCLASSIFIED_POD` | 명세에 없는 Pod가 돈다 | Job이면 끝나기를 기다린다. 계속 도는 Pod면 명세의 `workloads`에 역할을 적는다. 지우지 않는다 |
| `UNDECLARED_EGRESS` | 기존 정책이 다른 송신을 허용해 차단이 안 된다 | 그 정책의 담당자와 상의한다. 이 시험을 위해 지우지 않는다 |
| `HOST_NETWORK_POD` | 정책이 걸리지 않는 Pod다 | 멈춘다 |

2단계를 바로 이어서 하지 않으면 대조군을 지운다([되돌리기](#되돌리기)).

## 2. 차단 시험

**쓰기 대상이 목적지로 바뀐 뒤에 한다.** 위 1단계를 먼저 끝내 `plan.json`이 있어야 한다. 차단 전 측정부터 차단 뒤 측정까지 15분을 넘기면 `OBSERVATION_WINDOW_TOO_LONG`으로 UNKNOWN이니 처음부터 다시 한다.

### 1) 차단 전 측정

```sh
drill() { uv run python scripts/real_run.py --plan "$OUTPUT_DIR/plan.json" "$@"; }
drill collect --phase before --out "$OUTPUT_DIR/before.json"
```

### 2) 차단

적용 시각을 먼저 재서 적어 둔다. Pod의 시계로 잰다.

```sh
APPLIED_AT=$(drill clock)
uv run bravo-exit-drill manifests --plan "$OUTPUT_DIR/plan.json" > "$OUTPUT_DIR/policies.json"
kubectl apply --dry-run=server -f "$OUTPUT_DIR/policies.json"
kubectl apply -f "$OUTPUT_DIR/policies.json"
sleep 5
```

### 3) 재시작 두 번

새 Pod는 정책이 있어도 처음 잠깐 막히지 않는다(k3s에서 첫 연결이 매번 닿았다). 그래서 Pod를 교체해 이미 맺은 연결을 없앤 뒤, 그 Pod에서 컨테이너만 다시 띄워 차단된 채로 시작하게 한다. 이유는 [도구 안내](../../services/exit-drill/README.md#재시작을-두-번-하는-이유)에 있다.

```sh
kubectl -n target-dev rollout restart deployment/demo-chat deployment/target-pgbouncer
kubectl -n target-dev rollout status deployment/demo-chat --timeout=6m
kubectl -n target-dev rollout status deployment/target-pgbouncer --timeout=3m
sleep 5
drill restart --workload app --workload pooler --out "$OUTPUT_DIR/induced.json"
sleep 5
```

`rollout status`가 시간을 넘기면 앱이 차단 아래에서 뜨지 못하는 것일 수 있다. 멈추지 말고 다음 단계로 간다. 그것이 재려는 결과이고 `NOT_READY`로 FAIL이 된다.

풀러는 재시작 뒤 **첫 로그인이 맞는 비밀번호여야 한다**([T30 안내](../../infra/envs/target-workloads/README.md)). 옛 비밀번호를 가진 클라이언트가 있으면 먼저 멈춘다.

### 4) 목적지에 쓰기를 넣는다

역방향 복제가 살아 있는지는 LSN이 움직이는지로 본다. 앱 쓰기 스위치는 재시작으로 닫혔으므로 측정 담당자가 [쓰기 스위치](../../infra/envs/target-workloads/README.md#쓰기-스위치)로 열고 쓰기 요청을 보낸다. 넣지 못하면 건너뛴다. 판정은 `REPLICATION_IDLE`로 UNKNOWN이 되고, 나머지 결과는 그대로 읽을 수 있다.

### 5) 차단 뒤 측정과 판정

`cutover.json`은 T12 실행 기록이 `DONE`인 것을 직접 본 뒤에만 만든다. 모르면 만들지 않고 `--cutover`를 뺀다. 그러면 `CUTOVER_NOT_CONFIRMED`로 UNKNOWN이고, 그것이 맞는 결과다.

```sh
printf '{"writer": "target"}' > "$OUTPUT_DIR/cutover.json"
drill collect --phase after --policy-applied-at "$APPLIED_AT" \
  --cutover "$OUTPUT_DIR/cutover.json" --induced "$OUTPUT_DIR/induced.json" --out "$OUTPUT_DIR/after.json"

uv run bravo-exit-drill check --plan "$OUTPUT_DIR/plan.json" --before "$OUTPUT_DIR/before.json" \
  --after "$OUTPUT_DIR/after.json" > "$OUTPUT_DIR/report.json"; echo "종료 코드 $?"
uv run bravo-exit-drill map --inventory "$OUTPUT_DIR/inventory.json" --candidate onprem-demo \
  --plan "$OUTPUT_DIR/plan.json" --report "$OUTPUT_DIR/report.json" --format markdown > "$OUTPUT_DIR/residual-map.md"
```

풀러와 DB 이미지에는 Python이 없을 수 있어 그 위치는 `bash`로 같은 상태를 잰다. 앱과 대조군은 `probe.py`로 잰다. 모두 연결만 시도하고 아무것도 보내지 않는다. 복제 상태는 목적지 primary에서 읽기 전용 세션으로 `pg_stat_replication`을 읽는다.

그 뒤 바로 [되돌린다](#되돌리기).

## 결과 읽는 법

**종료 코드가 판정이다.** 0 PASS · 1 FAIL · 2 입력 오류 · 3 UNKNOWN. FAIL이 하나라도 있으면 FAIL이 UNKNOWN을 이긴다. 통과하지 못한 항목은 `report.json`의 `checks`와 지도 맨 위 **판정을 막은 항목**에 있다.

| 판정 | 코드 | 뜻 | 할 일 |
|---|---|---|---|
| UNKNOWN | `NOT_EXIT_EVIDENCE` · `AWS_ENDPOINT_REDIRECTED` | 호환 구성이다. 지금 앱에서는 **정상 결과**다 | T31 이미지 뒤 다시 잰다 |
| UNKNOWN | `ELIGIBILITY_NOT_READ` | 자격을 읽지 못했다 | 관측 파일과 도구 버전을 본다 |
| UNKNOWN | `CUTOVER_NOT_CONFIRMED` | 쓰기 대상을 확인하지 못했다 | T12 기록을 확인한다 |
| UNKNOWN | `STARTUP_NOT_ISOLATED` · `RESTART_NOT_OBSERVED` | 재시작 두 번이 빠졌다 | 3)부터 다시 한다 |
| UNKNOWN | `OTHER_CONTAINER_RESTARTED` | 앱은 정상인데 같은 Pod의 다른 컨테이너가 다시 떴다 | 그 컨테이너 로그를 본다. 차단 때문일 수 있다 |
| UNKNOWN | `REPLICATION_IDLE` | 측정 중 쓰기가 없었다 | 쓰기를 넣고 다시 잰다 |
| UNKNOWN | `BASELINE_NOT_STREAMING` | 차단 전에도 역방향 복제가 흐르지 않았다 | 컷오버 전이거나 복제 문제다. T8 담당자와 본다 |
| UNKNOWN | `POSITIVE_CONTROL_FAILED` · `BASELINE_NOT_OPEN` | 차단과 무관하게 대상에 안 닿는다. 장비에서 AWS로 나가는 길이 없을 수 있다 | 대조군에서 대상에 닿는지 본다 |
| FAIL | `SOURCE_PATH_OPEN` | **AWS나 출발지로 가는 길이 열려 있다** | CNI가 정책을 강제하는지, 다른 정책이 허용하는지 본다 |
| FAIL | `NOT_READY` · `NO_PODS` | 차단 아래에서 앱이 안 뜬다. 시작할 때 AWS가 필요한 앱이면 이것이 맞다 | 앱 로그를 본다 |
| FAIL | `LOCAL_PATH_BROKEN` · `NEW_CONNECTION_FAILED` | 내부 경로나 새 연결이 막혔다 | 빠진 내부 경로를 명세에서 찾는다 |
| FAIL | `REPLICATION_PATH_BROKEN` · `REPLICATION_NOT_STREAMING` | **복제가 끊겼다** | 즉시 되돌리고 T8 · T12 담당자에게 알린다 |

나머지 코드는 [도구 안내](../../services/exit-drill/README.md#판정)에 있다.

**PASS도 "AWS 경로가 막혔고 앱이 떠서 응답했다"까지만 말한다.** 첨부 읽기나 OpenBao 호출 같은 기능별 대체는 재지 않는다. `/ws`는 DB가 막혀도 연결을 받으므로, DB 경로는 `/api/health/db`로 읽는다.

지도는 위에서부터 먼저 볼 것 순서다. 항목은 어떤 경우에도 빠지지 않는다. 맨 위 **인벤토리 추적 메모**는 T16이 배포를 따라가며 남긴 메모이고, 못 읽은 입력도 여기 나온다. 못 읽은 입력이 있으면 아래 항목의 근거가 그만큼 좁다.

| 구역 | 뜻 |
|---|---|
| 남음 | 대체가 없거나, AWS 경로가 열렸거나, 외부 서비스(Cloudflare · Tailscale 등)다 |
| 끊김 | 남겨야 할 복제 채널이 시험에서 끊겼다 |
| 불명 | 등급을 모르거나 에이전트 확인을 기다린다. 추측으로 다른 구역으로 옮기지 않는다 |
| 유지 | 복제를 남긴 만큼 출발지와 아직 이어져 있다. 드릴을 통과해도 남는다 |
| 차단 대상 아님 | 정책을 걸지 않은 구성요소다. 이것을 거친 AWS 의존은 이 시험으로 못 잡는다 |
| 대체 있음 · 아직 확인 안 됨 | PASS가 아니면 모두 여기에 머문다. 호환 구성의 결과도 여기다 |
| AWS 경로 막힘 · 앱 동작 확인 | 그 경로가 막혔고 드릴 전체가 PASS다 |

## 되돌리기

**이 한 줄이면 차단 전으로 돌아간다.** 차단 정책과 대조군은 같은 레이블을 달고 있다. 다른 정책과 Pod는 건드리지 않는다. 시험이 끝났을 때와 이상이 보일 때 모두 이것을 쓴다.

```sh
kubectl -n target-dev delete networkpolicy,pod -l app.kubernetes.io/part-of=bravo-exit-drill
kubectl -n target-dev get networkpolicy,pod -l app.kubernetes.io/part-of=bravo-exit-drill   # 없어야 한다
```

삭제 뒤 새 연결은 바로 열린다(k3s 실측). 앱이 차단 때문에 뜨지 못했다면 kubelet이 다시 띄우며 회복한다. 기다리기 싫으면 `kubectl -n target-dev rollout restart deployment/demo-chat`을 쓴다. 앱 쓰기 스위치는 재시작으로 닫힌 채다. 측정 중이었다면 담당자가 다시 연다.

**바로 되돌리는 경우:** 복제가 끊겼다는 값이 보일 때, 다른 측정과 겹친 것을 알았을 때, 어느 단계에서든 상태를 확인할 수 없을 때다. 복제를 건드리지 않는 정책이므로 끊겼다면 다른 원인일 수 있다. 되돌린 뒤 T8 · T12 담당자와 본다.

## 시간 예상

**위 표의 시간은 실측이 아니라 추정이다.** 재 본 값은 임시 k3s(대역 앱)뿐이고, 개발 VM은 amd64 에뮬레이션이라 앱 시작이 훨씬 느리다. 앱의 startupProbe는 최대 5분을 기다리고, 이 시간이 대부분을 차지한다.

| 구간 | 임시 k3s 실측 | 실제 환경에서 늘어나는 곳 |
|---|---|---|
| 대조군 → 계획 → 지도 | 2.5초 | 대조군 이미지 시작. T16 인벤토리를 새로 만들면 그 시간이 더 든다 |
| 차단 전 측정 → 재시작 두 번 → 차단 뒤 측정 | 약 34초 | 앱 시작 두 번(Pod 교체 · 컨테이너 재시작) |
| 되돌리기 | 약 6초 | 없음 |

차단 시험에는 15분 제한이 있다. 앱 시작이 오래 걸려 넘기면 `OBSERVATION_WINDOW_TOO_LONG`으로 UNKNOWN이 나온다. 실제 시간을 재면 이 표를 고친다.

## 아직 재지 못한 것

- **실제 환경 전체.** RKE2 · Canal, 실제 demo-chat · PgBouncer · CNPG, 실제 Tailscale, 실제 AWS 경로. 강제 방식이 달라 새 Pod의 틈과 거부 모양이 다를 수 있다.
- **CNPG 이미지 안의 `bash` · `timeout` · `getent`.** 풀러 이미지는 실제로 쟀다. CNPG 이미지는 받아 보지 않았다. 없으면 그 위치가 `NOT_OBSERVED`나 `UNEXPECTED_STATE`로 UNKNOWN이 되고 PASS로 새지는 않는다.
- **실제 논리 복제의 `pg_stat_replication`.** 읽는 명령은 실제 PostgreSQL과 물리 복제 연결로 쟀다. 역방향 구독의 `application_name`은 `replication.py`의 값을 쓴다.
- **실제 앱의 재시작.** demo-chat의 1번 프로세스는 uvicorn이라 SIGTERM으로 끝날 것으로 보지만 재지 않았다. 안 끝나면 `drill restart`가 멈추고 기록을 남기지 않는다.
