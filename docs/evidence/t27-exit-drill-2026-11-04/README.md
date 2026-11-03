# T27 Exit Drill 로컬 실측 (2026-11-04)

**생성기 · 판정기 · 지도가 로컬 k3s에서 의도대로 동작한다. 실제 환경 실행은 아직이다.** 실제 k3s에 생성된 NetworkPolicy를 적용해 허용과 거부를 쟀고, 11개 시나리오가 모두 기대한 판정으로 나왔다. 앱 · 풀러 · DB · Tailscale · AWS는 전부 대역이다. 그래서 "목적지 앱이 AWS 없이 동작한다"는 아직 증명되지 않았고, T27 완료 기준도 T12 뒤의 실제 실행이 남아 있다.

| 검사 | 결과 |
|---|---|
| k3s 시나리오 | 11개 모두 기대한 판정 |
| 변이 시험(검사를 일부러 뺌) | 96개 모두 시험이 잡음 |
| 단위 시험 438개 · ruff · 공개 파일 검사 | 통과 |
| gitleaks(CI와 같은 v8.24.3) | `services/exit-drill` · 이 폴더 · ADR에서 없음 |
| 남은 자원 | `t27-` 컨테이너 · 네트워크 0개 |

## 시나리오 결과

`results.json`의 값이다. 각 시나리오는 차단 전 측정 → 정책 적용 → Pod 교체 → 컨테이너 재시작 → 측정 → 판정 순서로 돈다. 대조군은 정책이 걸리지 않은 Pod에서 앞뒤로 잰 연결이다.

| 시나리오 | 상황 | 판정 | 주요 코드 |
|---|---|---|---|
| `block_then_restart` | 정상 순서. AWS는 닫히고 복제와 앱은 동작 | **PASS** (23개 전부) | `DENIED` · `OPEN` · `STREAMING_ADVANCED` · `RESTARTED_READY` · `NEW_CONNECTION_OK` |
| `no_policy` | 정책 없이 재시작. 재는 길이 실제로 열려 있었다는 대조군 | FAIL | `SOURCE_PATH_OPEN` 6 |
| `policy_without_restart` | 정책만 넣고 재시작 안 함 | UNKNOWN | `RESTART_NOT_OBSERVED` |
| `additive_allow_leak` | 다른 정책이 외부를 허용해 합집합으로 AWS 경로가 남음 | FAIL | `SOURCE_PATH_OPEN` · `POLICY_WIDER_THAN_PLAN` |
| `replication_blocked` | 실수로 DB Pod까지 막음 | FAIL | `REPLICATION_PATH_BROKEN` · `REPLICATION_NOT_STREAMING` |
| `local_path_overblocked` | 허용 목록에서 풀러를 뺌. `/readyz`는 초록으로 남음 | FAIL | `LOCAL_PATH_BROKEN` · `NEW_CONNECTION_FAILED` |
| `app_needs_aws_at_start` | 시작할 때 AWS가 필요한 앱 | FAIL | `NOT_READY` |
| `pod_replacement_only` | 같은 앱을 Pod 교체만 함 | UNKNOWN | `STARTUP_NOT_ISOLATED` |
| `destination_down` | AWS 대역을 꺼서 거부처럼 보이게 함 | UNKNOWN | `POSITIVE_CONTROL_FAILED` |
| `idle_replication` | 쓰기가 없어 LSN이 멈춤 | UNKNOWN | `REPLICATION_IDLE` |
| `cni_does_not_enforce` | 정책을 강제하지 않는 클러스터. 정책 객체는 계획과 같음 | FAIL | `SOURCE_PATH_OPEN` · `POLICY_MATCHES_PLAN` |

생성기도 실제 클러스터 읽기로 거부했다. 앱 선택자를 공통 레이블로 쓰면 `REPLICATION_OWNER_SELECTED`와 `CONTROL_SELECTED`, 외부 허용 정책이 있으면 `UNDECLARED_EGRESS`다. PASS 상태에서 DB를 재시작해도 복제가 다시 이어졌다(`stream_reconnected_under_block`).

정상 시나리오의 [잔존 의존성 지도](residual-map.md)도 함께 있다. 남음 5, 불명 1(kms, 에이전트 확인 대기), 복제 유지 2, 차단 대상 아님 1, AWS 경로 막힘 · 앱 동작 확인 4건이다.

## 실측에서 나온 사실

k3s(kube-router) 값이다. RKE2의 Canal은 다를 수 있다.

1. **새 Pod는 정책이 이미 있어도 처음 잠깐 막히지 않는다.** 이 결과의 3회 모두 첫 연결이 닿았고, 첫 거부까지 0.66 · 0.31 · 0.41초 걸렸다(`new_pod_window`). 측정을 다섯 번(15회) 했는데 모두 첫 연결이 닿았고 범위는 0.20~0.88초였다. 이 폴더에는 마지막 결과만 있다.
2. **이미 맺은 연결은 정책을 넣어도 끊기지 않는다.** 앱이 시작할 때 연 연결이 차단 뒤에도 살아 있었다(`held_connection_alive_after_policy`). 새 연결은 적용 0.12초 뒤 거부됐다.
3. **같은 Pod에서 컨테이너만 다시 띄우면 시작 순간부터 막힌다.** 임시 측정에서 첫 시작은 연결, 재시작은 거부였다(기록은 이 폴더에 없다). `app_needs_aws_at_start`가 `NOT_READY`로 나오는 것이 같은 사실의 기록이다. 판정이 컨테이너 재시작을 요구하는 근거다.

## 고쳐 온 기록

**첫 측정의 거짓 PASS.** 시작할 때 AWS가 필요한 앱이 PASS로 나왔다([기록](first-run-false-pass.json)). 이 시험이 잡아야 할 바로 그 경우다. 새 Pod가 위 1번의 틈에 AWS에 닿아 떴기 때문이다. 판정이 Pod 교체만 보던 것을 고쳐, 정책에 묶인 Pod의 컨테이너 재시작을 요구하게 했다. 지금은 `NOT_READY`로 FAIL이다. Pod 교체만 하면 `STARTUP_NOT_ISOLATED`로 UNKNOWN이다.

**독립 검토.** 구현과 별개로 코드만 읽은 검토에서 잘못된 PASS와 거부 누락이 나왔다. 재현한 뒤 시험을 먼저 쓰고 고쳤다.

- 복제 경로에 역할을 적으면 재지 않은 의존성이 측정된 것처럼 보였다.
- 적용 시각이 사람이 적는 값뿐이라, 정책이 재시작보다 늦게 만들어져도 PASS였다. 클러스터의 생성 시각과 대조한다.
- 같은 목적지를 허용과 차단으로 함께 선언해도 만들어졌다(`BLOCK_ALSO_ALLOWED`).
- DB 수신 규칙이 다른 포트만 열어도 복제가 열린 것으로 읽었다.
- 값의 모양이 틀린 관측이 예외로 죽어 종료 코드 1, 즉 FAIL로 읽혔다. 이제 UNKNOWN이다.
- 지도가 다른 후보로 만들어지거나, 끊긴 복제를 유지로 보이거나, 모르는 판정값을 통과로 쳤다.
- 지도의 "대체 확인됨"은 증거보다 넓은 말이라 "AWS 경로 막힘 · 앱 동작 확인"으로 바꿨다.

**그 밖에.** 변이 시험에서 처음 살아남은 항목과, 넓게 잘못 읽어도 통과하던 `matchExpressions` 시험을 보강했다. gitleaks가 잡은 고정 WebSocket 값은 예외를 두지 않고 요청마다 만드는 난수로 바꿨다.

## 재지 못한 것

| 항목 | 이번에 쓴 것 | 왜 다를 수 있나 |
|---|---|---|
| RKE2 · Canal | k3s 1.35.5 · kube-router, 노드 1대 | 강제 방식이 달라 틈의 크기, 기존 연결 유지, 거부 모양(refused · timeout)이 다를 수 있다 |
| Tailscale | TCP 중계 대역 | ProxyGroup 실제 포트 · 태그 · ACL을 재지 않았다. 역방향 프록시 Pod 레이블(`target-db-tailnet`)은 **추정**이다 |
| AWS | Pod `/etc/hosts`로 대역에 묶음. 실제 AWS 접속 0회 | 실제 DNS 응답, 목적지 VM의 외부 경로를 재지 않았다. 차단 전에 실제 AWS가 안 닿으면 `BASELINE_NOT_OPEN`이다 |
| demo-chat · PgBouncer · CNPG | 표준 라이브러리 대역 | LSN은 시각에서 만든 값이고 `pg_stat_replication`이 아니다. 실제 앱의 1번 프로세스가 SIGTERM으로 끝나는지 재지 않았다 |
| 컷오버 | `cutover.writer`를 장치가 `target`으로 고정 | T12 결과와 연결되지 않았다 |
| 인벤토리 | T16 코드의 실제 출력, 단 T16 시험 입력 | 실제 demo-chat · 실제 AWS를 스캔한 결과가 아니다 |

## 이 시험이 잡지 못하는 것

- **기능별 대체.** 첨부 읽기 · OpenBao 호출처럼 앱이 실제로 대체 구현을 쓰는지는 재지 않는다. `/readyz` · `/api/health/db` · `/ws`만 본다.
- **간접 의존.** 차단은 앱과 풀러 Pod에만 건다. ESO · OpenBao · cloudflared처럼 다른 구성요소를 거쳐 AWS에 닿는 의존은 못 잡는다. 지도에 `not_blocked`로 일부 드러내지만 다른 네임스페이스는 보이지 않는다.
- **디스크 캐시.** 첫 시작 틈에 AWS에서 받은 값을 Pod 디스크(emptyDir)에 두는 앱은 컨테이너 재시작이 통과할 수 있다.
- **사이드카.** 재시작 횟수는 Pod의 컨테이너를 합산한다. 사이드카가 죽은 것과 시킨 재시작을 구별하지 않는다.
- **`local` 경로의 목적지.** 내부 Pod가 AWS로 중계하는 게이트웨이인지는 정적으로 알 수 없다. tailnet 네임스페이스와 레이블만 거부한다.

"AWS만" 막는 것이 아니라 선언 밖을 모두 막는 허용 목록이다([ADR-0014](../../adr/0014-exit-drill-allowlist.md)). 복제는 유지되므로 드릴을 통과해도 출발지와의 연결은 남는다.

## 실제 실행 전에 필요한 것

| 담당 | 필요한 것 |
|---|---|
| T12 | 컷오버 완료와 쓰기 대상을 읽는 방법(`cutover.writer`의 출처). 역방향 복제 상태(`pg_stat_replication`)를 어디서 어떤 계정으로 읽을지 |
| T12 · T15 · T31 | 목적지 앱의 실제 Pod 레이블과 Service 포트. 명세는 T12 기본값 `app=demo-chat`, 8080을 쓴다. T12의 풀러 기본값 `app=pgbouncer`와 T30의 `app=target-pgbouncer`도 맞춰야 한다 |
| T15 · T31 | 승인된 앱 이미지. 고정 소스 `ce5727e`는 시작할 때 Secrets Manager를 불러 FAIL이 정상이다 |
| T30 | 대조군 Pod 배치. `target-dev`의 다른 Pod와 기존 NetworkPolicy를 명세에 반영. 역방향 프록시 레이블 확인 |
| T8 · T30 (PR #131) | `network.py`가 만드는 이름은 `source-db-for-pooler`인데 `pooler.py` 오류 문구와 README는 `source-for-pooler`다. 명세는 렌더러를 따랐다 |
| T30 · T12 | 풀러 Pod 안에서 재는 방법. PgBouncer 이미지에 Python이 있는지 확인하지 않았다. 없으면 임시 컨테이너 권한이 필요하다 |
| T16 (PR #144) | `residual_dependencies`에 등급이 없다. 프로필에 설계 용어집의 Tailscale · GitHub · 모델 공급자가 없어 드릴 명세에 적었다. `digest`가 계약에는 있는데 스키마에서는 선택이다. PR의 `secrets` 검사가 시험 입력의 ARN과 UUID 때문에 실패 중이다. 이 폴더의 고정 입력은 그 값을 예시 값으로 바꿨다 |

## 재현과 파일

```sh
cd services/exit-drill
uv run python tests/integration/verify_k3s.py --out <폴더>        # 이미지 mirror.gcr.io/rancher/k3s:v1.35.5-k3s1, python:3.12-slim
uv run python tests/integration/mutation_check.py --out <파일>
```

| 파일 | 내용 |
|---|---|
| `results.json` | 시나리오별 판정 · 거부 코드 · 실측값 · 소스 해시 |
| `plan.json` | 차단 계획(결정과 NetworkPolicy) |
| `observation-before.json` · `observation-after.json` · `report-pass.json` | PASS 시나리오의 전후 관측과 판정 |
| `residual-map.md` · `residual-map.json` | 잔존 의존성 지도 |
| `mutation-results.json` | 변이 96개와 각각을 잡은 시험 |
| `first-run-false-pass.json` | 첫 측정의 거짓 PASS. 고치기 전 코드의 값이다 |

`source_sha256`은 측정에 쓴 소스의 해시이고, 서명이나 제3자 인증을 뜻하지 않는다. 관측 파일에 호스트 주소는 없다.
