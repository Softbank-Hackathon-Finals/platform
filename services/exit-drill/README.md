# Exit Drill (T27)

목적지 앱이 출발지(AWS) 없이 돌아가는지 시험하는 도구다(설계 FR-15). 앱의 AWS 접근만 끊고 복제 채널은 남긴 채, 재시작과 새 연결이 되는지 코드로 판정하고 남은 의존성을 지도로 낸다.

**이 도구는 계획과 판정만 만든다.** 클러스터에 접속하지 않고, 정책을 적용 · 삭제하지 않으며, Pod를 재시작하지 않는다. 그 일은 운영자가 한다. 접속 시험(`probe.py`)만 네트워크에 닿고, 정해진 대상에 연결만 시도한다. LLM은 쓰지 않는다.

실제 목적지에서 돌리는 순서는 [실행 안내](../../docs/operations/t27-exit-drill-real-run.md)에 있다. kubectl을 부르는 일은 패키지 밖의 운영자 도구 [`scripts/real_run.py`](scripts/real_run.py)로 모았다. 읽기와 접속 시험을 하고, 상태를 바꾸는 것은 컨테이너 재시작뿐이다. 정책 적용 · 되돌리기는 그 도구에도 없다.

```
입력                          도구                     결과
드릴 명세(profiles/) ─┐
T16 인벤토리 ─────────┼─→ render ──→ 계획(결정 + NetworkPolicy) ──→ 운영자가 적용 · 재시작
클러스터 읽기 ────────┘                │
                                       ├─→ targets · probe · assemble ──→ 차단 전후 관측
                                       ├─→ check ──→ PASS / FAIL / UNKNOWN
                                       └─→ map ───→ 잔존 의존성 지도
```

## 구성

| 모듈 | 하는 일 |
|---|---|
| `spec.py` | 드릴 명세 읽기. 어떤 경로가 무슨 경로인지 사람이 선언한 입력이다 |
| `inventory_input.py` | T16 인벤토리 읽기. **T16의 필드와 값(등급 · 역할 · 환경 종류)을 아는 곳은 이 파일뿐이다.** 계약이 제안 상태라 바뀌면 여기만 고친다 |
| `aws_endpoints.py` | AWS 서비스 이름 → 시험할 공개 주소 |
| `cluster.py` | `kubectl get -o json` 결과와 NetworkPolicy 해석 |
| `policy.py` | 경로별 결정(ALLOW · BLOCK · KEEP)과 NetworkPolicy 생성. 위험한 입력은 만들지 않고 실패한다 |
| `probe.py` | Pod 안에서 대상을 재는 접속 시험. 파일 하나 · 표준 라이브러리만 쓴다 |
| `observation.py` | 읽은 결과를 판정 입력으로 묶는다. 모르는 값은 모른다고 둔다 |
| `verdict.py` | 차단 전후 관측으로 PASS · FAIL · UNKNOWN을 정한다 |
| `residual_map.py` | 잔존 의존성 지도(JSON · Markdown) |
| `control_pod.py` | 대조군 Pod 렌더. 계획을 만들기 전에 띄운다 |

## 차단 방식

앱과 풀러 Pod의 **송신을 선언한 목적지 내부 경로로만 좁히는 허용 목록**이다. 이유는 [ADR-0014](../../docs/adr/0014-exit-drill-allowlist.md). 그 결과 경로는 셋으로 나뉜다.

| 분류 | 결정 | 예 | 정책에서 |
|---|---|---|---|
| `local` | ALLOW | DNS, 앱→풀러, 앱→SeaweedFS, 풀러→목적지 DB | 허용 목록에 넣는다 |
| `source_access` | BLOCK | AWS API, 풀러→출발지 DB 프록시 | 허용 목록에서 뺀다 |
| `replication` | KEEP | 목적지 DB↔출발지 DB(Tailscale) | DB Pod를 고르지 않는다 |

AWS 경로는 명세에 적지 않는다. **인벤토리가 쓴다고 한 서비스마다 자동으로 차단 시험 대상이 된다.** 같은 입력이면 같은 계획이 나오고, 경로 순서를 바꿔도 해시가 같다. 재시작으로 Pod UID가 바뀌어도 같은 계획이다.

**밖에서 들어오는 경로(`edge` 역할)는 정책으로 고르지 않는다.** Cloudflare Tunnel connector(`cloudflared`)처럼 공개 트래픽을 앱 Service로 넘기는 Pod다. 송신을 좁히면 공개 경로가 끊긴다. 명세에 그 Pod가 기대는 잔존 의존성(`residual`)을 적으면 지도의 그 항목 "목적지 구현" 칸에 나온다. 시험 대상이 아니라서 터널이 내려가 있어도 드릴은 돈다.

## 만들지 않고 실패하는 경우

복제를 끊거나 AWS 경로를 남기는 입력은 `render`가 거부한다(종료 코드 1). 위반을 한 번에 모두 알린다.

| 코드 | 뜻 |
|---|---|
| `REPLICATION_OWNER_SELECTED` | 앱 선택자가 DB Pod까지 고른다. 공통 레이블(`component=target`)로 고르면 생긴다 |
| `BLOCK_HITS_REPLICATION` | 차단 경로가 복제 경로와 같다 |
| `BLOCK_ALSO_ALLOWED` | 같은 Pod에게 막는다면서 허용 목록에도 넣었다. 허용이 이긴다 |
| `REPLICATION_BLOCKED` | 기존 정책이 이미 복제 경로를 막고 있다. 정방향은 DB 송신, 역방향은 DB 수신(5432)으로 본다 |
| `REPLICATION_NOT_TAILNET` · `REPLICATION_NOT_POSTGRES` | 복제로 선언했지만 Tailscale 5432가 아니다 |
| `UNDECLARED_EGRESS` | 기존 정책이 계획 밖 허용을 준다. NetworkPolicy는 합집합이라 새 정책을 더해도 AWS 경로가 남는다 |
| `HOST_NETWORK_POD` | hostNetwork Pod에는 NetworkPolicy가 걸리지 않는다 |
| `UNCLASSIFIED_POD` | 명세에 없는 Pod가 돈다. 차단 범위 밖이라 AWS에 닿을 수 있다 |
| `LOCAL_ROUTE_LEAVES_CLUSTER` | 내부 경로라면서 외부 주소나 tailnet 프록시를 가리킨다 |
| `SELECTOR_MATCHES_NOTHING` | 오타 난 선택자의 정책은 조용히 아무것도 막지 않는다 |
| `CONTROL_SELECTED` · `NO_CONTROL` | 대조군이 차단되거나 없으면 대상이 살아 있는지 잴 수 없다 |
| `EDGE_SELECTED` | 앱 선택자가 터널(`edge`) Pod까지 고른다. 차단하면 공개 경로가 끊긴다 |
| `UNKNOWN_RESIDUAL` | `edge`가 잇는 잔존 의존성이 인벤토리에도 명세의 `declared_residuals`에도 없다 |
| `CONTAINER_NOT_IN_POD` | 명세의 앱 컨테이너 이름이 Pod에 없다. 드릴을 다 돌린 뒤 판정 불가가 되기 전에 멈춘다 |
| `OWNER_ROLE_MISMATCH` · `UNKNOWN_WORKLOAD` · `DUPLICATE_ROUTE` · `POLICY_NAME_CONFLICT` · `NO_REPLICATION_ROUTE` · `NO_SOURCE_ROUTE` · `NO_AWS_CLIENT` · `EMPTY_SELECTOR` | 명세 자체의 모순 |

## 판정

- **FAIL**: 잘못됐다는 증거가 있다. AWS 경로가 열렸거나(`SOURCE_PATH_OPEN`), 복제가 끊겼거나(`REPLICATION_PATH_BROKEN` · `REPLICATION_NOT_STREAMING`), 앱이 뜨지 않았거나(`NOT_READY` · `NO_PODS`), 내부 경로나 새 연결이 막혔다(`LOCAL_PATH_BROKEN` · `NEW_CONNECTION_FAILED`). 합집합으로 허용이 남거나(`POLICY_WIDER_THAN_PLAN`) 정책이 안 걸리는 Pod가 있어도(`HOST_NETWORK_POD`) 실패다.
- **UNKNOWN**: 증거가 빠졌거나 원인을 가를 수 없다. 아래가 그런 경우다.
- **PASS**: 필요한 증거가 모두 있고 전부 맞을 때만이다. FAIL이 하나라도 있으면 FAIL이 UNKNOWN을 이긴다.

| 불명확한 상황 | 코드 | 왜 통과가 아닌가 |
|---|---|---|
| 대조군이 대상에 못 닿음 | `POSITIVE_CONTROL_FAILED` | 대상이 내려가 있어도 거부처럼 보인다 |
| 차단 전에도 닫혀 있었음 | `BASELINE_NOT_OPEN` | 지금 닫힌 것이 정책 덕분인지 모른다 |
| 이름 풀이 실패 | `UNEXPECTED_STATE` | DNS 고장과 구별되지 않는다 |
| Pod UID가 그대로 | `RESTART_NOT_OBSERVED` | 재시작 시험을 하지 않은 것이다. 이미 맺은 연결은 정책을 넣어도 끊기지 않는다 |
| Pod 교체만 함, 또는 앱 대신 사이드카만 다시 뜸 | `STARTUP_NOT_ISOLATED` | 새 Pod는 처음 잠깐 정책에 묶이지 않는다. 아래 "재시작을 두 번 하는 이유" |
| 정책 생성 시각이 재시작보다 늦거나 모름 | `STARTED_BEFORE_BLOCK` · `POLICY_TIME_UNKNOWN` | 적용 시각은 사람이 적는 값이라 클러스터 기록과 대조한다 |
| 시키지 않은 재시작이 있음 | `UNSTABLE_RESTARTS` | 앱이 죽었다 살아난 것일 수 있다 |
| 앱 컨테이너 상태가 없거나 이름이 모호함 | `CONTAINER_UNKNOWN` | 어느 컨테이너의 값인지 모른다 |
| 재시작 기록이 관측과 다름 | `RESTART_RECORD_MISMATCH` | 기록한 컨테이너가 끝나지 않았거나 다른 컨테이너다 |
| 같은 Pod의 다른 컨테이너가 다시 뜸 | `OTHER_CONTAINER_RESTARTED` | 앱의 근거가 아니고, 차단 아래에서 죽은 것일 수 있다 |
| 계획을 만든 뒤 명세에 없는 Pod가 뜸 | `POD_OUTSIDE_PLAN` | 어느 정책에도 안 걸리고 아무도 재지 않는다 |
| LSN이 안 움직임 | `REPLICATION_IDLE` | 쓰기가 없으면 채널이 살아 있는지 모른다 |
| 쓰기 대상이 목적지로 확인 안 됨 | `CUTOVER_NOT_CONFIRMED` | 컷오버 전이면 이 시험의 의미가 없다 |
| 출구 시험 대상이 아닌 Pod | `NOT_EXIT_EVIDENCE` · `AWS_ENDPOINT_REDIRECTED` | 호출이 AWS로 가지 않으면 막아도 멀쩡해 보인다. 차단을 잰 것이 아니다 |
| 그 여부를 읽지 못함 | `ELIGIBILITY_NOT_READ` | 읽지 못한 것을 자격 있다고 하지 않는다 |
| 다른 계획의 관측 · 손댄 계획 | `PLAN_NOT_BOUND` · `PLAN_TAMPERED` | 이 시험의 증거가 아니다 |
| 측정 중 정책이 바뀜 | `POLICY_CHANGED_DURING_OBSERVATION` | 무엇을 잰 것인지 모른다 |
| 관측 값의 모양이 틀림 | `OBSERVATION_UNREADABLE` | 읽지 못한 증거로 판정하지 않는다 |
| 잴 수 없는 의존성 | `UNMAPPED_DEPENDENCY` | 차단을 확인할 경로가 없다 |

**거부 하나를 세는 조건**은 넷이다. 대상 Pod가 거부되고, 그 Pod의 차단 전 측정은 연결됐고, 차단되지 않은 대조군이 재기 전과 후 모두 연결되고, 잰 Pod가 그 워크로드 소속이다.

### 재시작을 두 번 하는 이유

"AWS 없이 뜬다"를 보려면 앱이 시작하는 순간 이미 차단돼 있어야 한다. 그런데 새 Pod는 정책이 미리 있어도 처음 잠깐은 막히지 않는다. [쿠버네티스 문서](https://kubernetes.io/docs/concepts/services-networking/network-policies/#pod-lifecycle)도 정책 처리가 끝나기 전에 뜬 Pod는 보호 없이 시작할 수 있다고 적는다. k3s에서 재 보니 첫 연결은 매번 AWS에 닿았다.

이 틈 때문에 시작할 때 AWS를 부르는 앱이 첫 측정에서 PASS로 나왔다. 그래서 두 단계로 재시작한다.

1. **Pod 교체**(`rollout restart`): 정책을 건 뒤 Pod를 새로 만든다. 이미 맺은 연결을 모두 없앤다.
2. **앱 컨테이너만 재시작**(`scripts/real_run.py restart`): Pod와 IP가 그대로라 이미 정책에 묶여 있다. 앱은 차단된 채로 시작한다. 죽이기 전의 컨테이너 ID를 기록으로 넘긴다. 신호는 그 컨테이너의 모든 프로세스에 보낸다. 풀러 이미지는 1번 프로세스가 `sh`이고 처리기가 없어 1번에만 보내면 무시된다(실제 이미지에서 쟀다).

판정은 두 번째가 있어야 `restart:*`를 통과시키고, 새 연결도 그 뒤에 맺은 것만 센다. 컨테이너 재시작은 정책 생성과 Pod 시작 양쪽에서 2초 이상 지난 뒤여야 한다. 그 전이면 아직 틈 안일 수 있어서다.

**판정 대상은 명세가 앱 역할마다 적은 컨테이너(`workloads.*.container`) 하나다.** 이 이름은 계획 해시에 들어간다. 재시작 횟수 · 시작 시각 · 컨테이너 ID는 그 컨테이너의 값만 쓰고, 끝난 컨테이너의 ID(`lastState.terminated.containerID`)가 기록과 같고 지금 도는 ID와 달라야 통과한다. 빈 값이나 공백뿐인 ID는 없는 값으로 친다. 사이드카가 다시 뜬 것은 앱의 근거가 되지 않는다. 그렇다고 넘기지도 않아서, 앱은 통과 조건을 채워도 `OTHER_CONTAINER_RESTARTED`로 UNKNOWN이다.

### 출구 시험 자격

T30의 개발 측정용 앱은 같은 Pod의 호환 컨테이너가 Secrets Manager 호출에 응답한다([ADR-0017](../../docs/adr/0017-target-app-secrets-manager-compat.md)). AWS를 막아도 앱이 떠서 거짓 PASS가 나올 수 있다.
그래서 수집기가 앱 역할 Pod마다 `bravo.t30/not-exit-evidence` 표지와 모든 컨테이너(재시작 가능한 사이드카 포함)의 `AWS_ENDPOINT_URL*` 환경 변수를 읽어 `exit_blockers`로 적는다.
판정은 차단 전후의 Pod를 모두 보고, 하나라도 걸리면 `eligibility:*`가 UNKNOWN이다. 값을 읽지 못해도 UNKNOWN이다. 표지만 지워도 환경 변수가 남으면 통과하지 못한다.
`envFrom`이 있는 Pod도 `ELIGIBILITY_NOT_READ`다. Pod만 봐서는 어떤 변수가 들어오는지 모르고, 참조한 ConfigMap · Secret을 새로 읽지 않는다. 쓰려면 환경 변수를 `env`에 직접 적는다. 볼륨으로 넣은 AWS 설정 파일은 보지 않는다.

종료 코드: 0 PASS · 1 FAIL(또는 계획 거부) · 2 입력 오류 · 3 UNKNOWN.

## 잔존 의존성 지도

인벤토리는 이전 전에 찍은 사진이라 그것만으로 "이제 없다"고 말할 수 없다. 그래서 등급과 시험 결과를 함께 본다. 항목은 어떤 경우에도 빠지지 않는다.

| 상태 | 뜻 |
|---|---|
| `remains` 남음 | 대체 없음(`none`), AWS 경로가 열림, 또는 외부 서비스(Cloudflare 등) |
| `broken` 끊김 | 남겨야 할 복제 채널이 시험에서 끊겼다 |
| `unknown` 불명 | 등급 미지, 에이전트 확인 대기, 목적지 구현 없음, 잴 경로 없음 |
| `kept` 유지 | 복제를 남긴 만큼 출발지와 아직 이어져 있다는 사실 |
| `not_blocked` 차단 대상 아님 | 정책을 걸지 않은 구성요소. 이것을 거친 AWS 의존은 이 시험으로 못 잡는다 |
| `unverified` 확인 안 됨 | 대체는 있지만 드릴을 통과하지 못했거나 돌리지 않았다 |
| `blocked_app_ok` AWS 경로 막힘 · 앱 동작 확인 | 해당 경로가 막혔고 드릴 전체가 PASS다 |

`blocked_app_ok`는 "AWS 경로가 막혔고 앱이 떠서 응답했다"까지만 뜻한다. 첨부 읽기나 OpenBao 호출처럼 기능별로 대체 구현을 쓰는지는 이 시험이 재지 않는다.

외부 서비스에는 T16 계약상 등급이 없다. 지어내지 않고 `not_graded`(등급 없음)로 둔다.

드릴을 돌리지 않아도 지도는 나온다(`--report` 없이, 판정 `NOT_RUN`). 판정이 통과하지 못했으면 그 이유를 `drill_not_pass`에, T16이 후보에 없다고 한 기능을 `candidate_missing_capabilities`에 함께 적는다.

T16이 배포를 따라가며 남긴 메모(`deploy.notes`)는 `inventory_trace_notes`에 그대로 옮기고, Markdown 맨 위 **인벤토리 추적 메모**에 보인다. 출발지 내보내기를 못 읽었으면 여기에 나온다. 못 읽은 입력이 있으면 아래 항목의 근거가 그만큼 좁다. T16 스키마가 요구하는 필드라 없거나 글자 목록이 아니면 인벤토리를 받지 않는다.

## 실행

```sh
uv sync --locked && uvx ruff==0.17.3 check . && uv run --locked pytest -q
```

아래는 명령 하나하나의 입력과 출력이다. 실제로 돌릴 때는 [실행 안내](../../docs/operations/t27-exit-drill-real-run.md)를 따른다. 컷오버(T12)가 끝나 쓰기가 목적지에 있어야 차단 시험의 의미가 있다. 대조군 Pod(`bravo-exit-drill-control=true`)는 `control-pod`로 만들어 미리 띄운다.

```sh
bravo-exit-drill control-pod --spec profiles/target-dev.json --image <digest로 고정한 이미지>
bravo-exit-drill render --spec profiles/target-dev.json --inventory inventory.json \
  --candidate onprem-demo --snapshot snapshot.json > plan.json   # snapshot: kubectl get pods,networkpolicies -o json
bravo-exit-drill manifests --plan plan.json > policies.json       # 운영자가 검토 후 적용
bravo-exit-drill targets --plan plan.json --vantage app           # 위치별 대상. Pod 안에서 probe.py로 잰다
bravo-exit-drill assemble --plan plan.json --phase before ...     # 적용 전 관측
#   운영자: 정책 적용 → 몇 초 기다림 → 앱 · 풀러 Pod 교체 → 몇 초 기다림 → 앱 컨테이너만 재시작 → 다시 잼
bravo-exit-drill assemble --plan plan.json --phase after --policy-applied-at <적용 시각> \
  --induced-restarts induced.json ...   # {Pod UID: {"container": 이름, "replaced_container_id": 죽이기 전 ID}}
bravo-exit-drill check --plan plan.json --before before.json --after after.json > report.json
bravo-exit-drill map --inventory inventory.json --candidate onprem-demo --plan plan.json \
  --report report.json --format markdown
```

Pod의 `startTime`은 초 단위라 적용 직후 같은 초에 재시작하면 `STARTED_BEFORE_BLOCK`이 된다. 적용 시각은 Pod와 같은 시계로 잰다. 시험이 끝나면 운영자가 `exit-drill-egress-*` 정책을 지워 되돌린다. 도구에는 삭제 명령이 없다.

## k3s 시험과 변이 시험

Docker로 실제 k3s를 띄워 생성된 정책을 적용하고 허용 · 거부를 잰다. 수동 실행이고 CI에 넣지 않는다. 컨테이너는 전부 `t27-`로 시작하고 끝나면 지운다. AWS 이름은 Pod의 `/etc/hosts`로 대역에 묶으며 실제 AWS에 접속하지 않는다. 결과에 측정 소스의 커밋을 남기므로 소스를 커밋한 뒤에 돌린다.

```sh
uv run python tests/integration/verify_k3s.py --out <폴더>
uv run python tests/integration/verify_real_run.py --out <폴더>   # 실행 안내의 순서를 그대로 돌린다
uv run python tests/integration/mutation_check.py --out mutation-results.json
```

결과와 재지 못한 것은 [증거 문서](../../docs/evidence/t27-exit-drill-2026-11-04/README.md)와 [실행 준비 증거](../../docs/evidence/t27-real-run-prep-2026-11-06/README.md)에 있다. 실제 `target-dev`의 1단계 결과는 [실제 지도 증거](../../docs/evidence/t27-real-map-2026-11-06/README.md)에 있다.

## 명세 값의 출처

`profiles/target-dev.json`의 값은 지금 코드에서 읽었다. `tests/test_real_target.py`가 저장소 렌더러의 실제 출력과 대조한다. 실제 환경에서 맞지 않으면 `render`가 멈춘다.

| 값 | 출처 | 상태 |
|---|---|---|
| 앱 `app=demo-chat`, 8080 | T12 `sites.py` 기본값 · 모의 환경 | 실제 목적지 배포는 T15 · T31 뒤에 확인 |
| 앱 컨테이너 `app` | 출발지 배포 `infra/envs/source-workloads/render.py` | 목적지 배포에서 같은지 T15 · T31 뒤에 확인 |
| 풀러 `app=target-pgbouncer`, 컨테이너 `target-pgbouncer`, Pod 포트 5432 | T30 `pooler.py` | PR #131 |
| DB `cnpg.io/cluster=target-db` | T30 `render.py` | PR #131 |
| 객체 저장소 `target-seaweedfs`, 8333 | T30 `objects.py` | PR #131 |
| Edge 터널 `app=bravo-edge-tunnel` → 잔존 `cloudflare (edge · tunnel)` | T17 `infra/envs/edge-tunnels/render.py` | PR #383. 2026-11-06 실제 Pod 레이블과 같음 |
| 프록시 `bravo-target-pooler` · `bravo-target-replication` | T8 `network.py` | PR #131 |
| 역방향 프록시 레이블 `target-db-tailnet` | Tailscale operator 규칙에서 추정 | 2026-11-06 `tailscale-system`의 실제 Pod에 있음 |
| 외부 잔존 3건(Tailscale · GitHub · 모델 공급자) | 설계 부록 용어집 | T16 프로필에 없어 명세에 적음 |
