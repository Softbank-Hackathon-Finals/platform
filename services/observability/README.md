# 관측: 판정 기준 구현(T36)과 환경 횡단 지표 수집(T37)

## 판정 기준 구현 (T36)

`src/bravo_observability`는 [판정용 관측 계약](../../docs/observability/README.md)의 기준 구현이다. 표준 라이브러리만 쓰고, LLM · 네트워크 · 시계를 쓰지 않는다.

| 함수 | 누가 | 하는 일 |
|---|---|---|
| `build_window(records, plan_id=, start=, end=, pools=)` | T18 부하 생성기 | 한 창의 요청 결과(`Record`)를 `SliWindow`로 요약한다. `pools`는 `X-Bravo-Env` 값 → `source` · `target` |
| `receive(window, now)` | 플랫폼 수신 경로 | `received_at`을 플랫폼이 받은 시각으로 덮어쓴다 |
| `judge(profile, window, plan_id=, state=, weight_target=, observe_from=, decided_at=, prior_unknown=)` | T17 · T10 | `SliJudgement`를 돌려준다. 판정 · 사유 · 조치와 입력 창 원본을 담는다 |

```sh
cd services/observability && uv sync --locked && uv run --locked pytest
```

시험은 계약 시험 전체(판정 25건, 스키마 거부 8건)와 창 요약 생성 규칙(환경 분류, nearest-rank p95, 빈 환경, 수신 시각 덮어쓰기)을 돈다.

## 환경 횡단 지표 수집 (T37)

출발지 · 목적지 클러스터마다 OpenTelemetry Collector를 두고, 앱 지표를 플랫폼 지표 저장소(Prometheus)로 보낸다. 결정 배경은 [ADR-0010](../../docs/adr/0010-observability-boundary.md)이다. **판정 입력이 아니다.** SHIFT · WATCH 판정은 T18 창 요약만 쓴다([판정용 관측 계약](../../docs/observability/README.md)).

| 경로 | 내용 |
|---|---|
| [`deploy/collector/`](deploy/collector/) | 클러스터별 Collector: 설정(`collector.yaml`) · 배포 매니페스트 · NetworkPolicy. `kubectl apply -k`로 적용 |
| [`deploy/platform/`](deploy/platform/) | 플랫폼 지표 저장소: 설정(`prometheus.yml`) · mTLS(`web.yml`) · 알림 규칙과 단위 시험(`rules/`) · 배포 매니페스트 |
| [`testing/run.sh`](testing/run.sh) | Docker compose 재현 시험: mTLS, 위조 · 민감 속성, 디스크 큐, 생존 신호 사칭, 알림 |
| [`testing/k8s.sh`](testing/k8s.sh) | 쿠버네티스 배포 시험: Docker 안 k3s(T7 RKE2와 같은 1.35)에 매니페스트를 그대로 적용 |
| [런북](../../docs/observability/runbooks.md) | 알림마다 뜻 · 확인 순서 · 조치 |

이미지는 태그와 digest로 고정한다.
- `otel/opentelemetry-collector-contrib:0.164.0`: T34 감사 전달(#117)과 같은 digest
- `prom/prometheus:v3.15.1`

### 동작

- **환경 이름은 Collector가 붙인다.** 앱이 보낸 `bravo.env`는 허용 목록에서 지워지고, Collector의 `BRAVO_ENV`로 덮어쓴다. 앱이 다른 환경을 사칭할 수 없다.
- **허용 목록:** 앱 지표는 아래 속성만 남는다. 채팅 본문, `Authorization`, 토큰, SQL 파라미터는 저장되지 않는다.
  - 리소스 속성: `service.name` · `service.instance.id`
  - 데이터 포인트 속성: `http.request.method` · `http.response.status_code` · `http.route`
- **수집 경로 생존 신호:** Collector가 자기 지표를 10초마다 긁어 같은 경로로 보낸다. 이 경로에서만 신뢰 라벨 `bravo.signal=collector-self`를 붙인다. 앱 경로는 허용 목록이 이 속성을 지우므로, 앱이 `service.name=bravo-collector`나 지표 이름 `up`을 써도 생존 신호로 세지 않는다.
  - 최근 수신이 30초(수집 간격의 3배)를 넘거나 5분 조회 범위 안에 아예 없으면 그 환경은 "확인 불가"이고 `BravoEnvUnobservable` 알림이 뜬다.
  - 보내는 방식이라 데이터가 오지 않는 것을 "정상"으로 읽지 않는다.
- **유실 내구성:** 플랫폼에 보내지 못한 지표는 디스크 큐(5000건)에 쌓이고, 지표 저장소가 늦게 도착한 표본을 받는 10분 동안 재시도한다. Collector가 재시작해도 큐는 남는다.
- **알림:** [`rules/alerts.yml`](deploy/platform/rules/alerts.yml)에 규칙 4개를 코드로 둔다.
  - 확인 불가, 전송 실패, 대기열 포화, 수신 거부
  - 모두 생존 신호 경로의 지표만 보고, `promtool test rules` 단위 시험과 [런북](../../docs/observability/runbooks.md)이 있다.
- **시계:** 생존 신호 시각은 Collector 호스트 시계다. 호스트 시계가 앞서면 그만큼 확인 불가 판정이 늦어지므로 노드는 시간 동기화를 켠다.
- **보존:** 3일 또는 4GB. 판정의 근거는 판정 기록(`SliJudgement`)에 따로 남으므로 지표 저장소를 지워도 판정은 다시 계산할 수 있다.

### 보안 경계

- **mTLS:** 지표 저장소는 쓰기(OTLP)와 조회 모두 클라이언트 인증서를 요구한다.
  - 같은 CA가 발급하고 이름이 허용 목록(`web.yml`의 `client_allowed_sans`)에 있어야 한다.
  - TLS 1.3만 받는다.
- **남는 위험:** 인증서 이름은 환경 이름과 묶이지 않는다. 한 Collector 인증서가 유출되면 다른 환경 이름으로 서버 지표를 넣을 수 있다. 이 지표는 판정 입력이 아니고(ADR-0010), 인증서는 Collector Pod 밖으로 나가지 않는다.
- **NetworkPolicy:**
  - Collector는 `bravo.observability/send=true` 라벨을 붙인 네임스페이스의 앱 지표만 받는다. 나가는 연결은 DNS와 지표 저장소 포트(9090 · 30990)만 허용한다.
  - 지표 저장소는 같은 네임스페이스의 Collector, `bravo.observability/platform-client=true` 라벨 네임스페이스(콘솔 API), Tailscale 대역만 받는다. 나가는 연결은 없다.
  - 네임스페이스 라벨은 `kubectl label`로 붙인다. 다른 Namespace 매니페스트를 apply하면 거기 없는 라벨이 지워진다(k8s 시험에서 실제로 재현됨).
- **Pod 보안:** 두 워크로드 모두 Pod 보안 표준 restricted를 만족한다(비루트, 읽기 전용 루트 파일시스템, 권한 상승 금지, 모든 capability 제거, RuntimeDefault seccomp). ServiceAccount 토큰은 붙이지 않는다.
- **배포 전에 만들 것 (git에 두지 않음):** Secret `bravo-collector-tls` · `bravo-prometheus-tls`(플랫폼 CA 발급), ConfigMap `bravo-collector-env`(`BRAVO_ENV`, `PLATFORM_OTLP_ENDPOINT`). 시험은 실행할 때 임시 CA를 만들고 끝나면 지운다.
- 리허설 네임스페이스에는 Collector로 가는 송신 예외를 열지 않는다(ADR-0002).

### 검증 (2026-11-04 로컬)

```sh
services/observability/testing/run.sh   # Docker compose, 약 1분 반
services/observability/testing/k8s.sh   # Docker 안 k3s, 약 2분 반
```

| 시험 | 확인 | 결과 |
|---|---|---|
| `run.sh` | 허용된 이름의 인증서로 조회 성공. 인증서 없음 · 다른 CA · 허용 목록 밖 이름은 거부 | 4/4 통과 |
| | 위조한 `bravo.env=aws`가 `onprem`으로 기록, `http.route`는 남음, 카나리 문자열 0건 | 3/3 통과 |
| | 생존 신호 1초 미만. 저장소가 멈춘 동안 보낸 지표가 Collector 재시작 뒤 도착 | 2/2 통과 |
| | 앱이 생존 신호를 사칭한 뒤 Collector 중지: 45초 뒤 확인 불가, 사칭 시계열엔 신뢰 라벨 없음, `BravoEnvUnobservable` firing | 3/3 통과 |
| `k8s.sh` | restricted 네임스페이스에서 두 워크로드 준비 | 통과 |
| | 라벨 붙인 네임스페이스의 지표가 Collector → mTLS → 저장소로 도착, 환경은 `onprem` | 2/2 통과 |
| | 라벨 없는 네임스페이스는 Collector로 보내기 · 저장소 연결 모두 차단 (같은 연결이 허용 라벨에서는 성공하는 대조군 포함) | 3/3 통과 |
| | Collector를 0개로 줄이면 `BravoEnvUnobservable` firing | 통과 |
| `promtool` | `test rules`: 확인 불가(사칭 시계열 무시, 5분 뒤에도 같은 라벨), 처음부터 보고 없는 환경, 전송 실패, 대기열, 거부 | 통과 |
| `trivy config` | 두 매니페스트 HIGH · CRITICAL 0건 | 통과 |

일부러 망가뜨린 설정으로도 돌려, 시험이 보호 장치를 실제로 확인하는지 봤다.
- 허용 목록을 빼면 카나리 검사가 실패한다.
- 환경 덮어쓰기까지 빼면 위조 검사도 실패한다.
- 생존 신호를 `job` 라벨로 고르면 사칭 검사가 실패한다.
- 디스크 큐를 메모리 큐로 바꾸면 재시작 뒤 도착한 시계열이 0개라 큐 검사가 실패한다.
- 알림 규칙에서 신뢰 라벨 조건을 빼거나 `absent` 줄을 빼면 `promtool` 시험이 실패한다.

### 남은 일

- **실제 환경 배포:** 플랫폼 RKE2(T7), 출발지 EKS(T6), 목적지 RKE2(T30). 매니페스트는 그대로 적용할 수 있고, 남은 것은 배포 경로 · 클러스터 접근 · 인증서 발급이다.
- **디스크 큐 · 저장소 영속성:** 지금은 emptyDir이라 Pod가 다른 노드로 옮겨지면 잃는다. 저장소 클래스가 정해지면 PVC로 바꾼다.
- **나가는 연결 좁히기:** Collector의 나가는 연결은 지금 포트만 제한한다. 환경별로 플랫폼 주소 ipBlock을 패치로 더한다. 지표 저장소에는 출발지 VPC 주소 ipBlock을 더한다.
- **앱 지표:** 데모 앱에 OTel 계측이 없어 지금은 생존 신호와 Collector 자체 지표만 들어온다.
- **DB 지표:** CNPG(9187) 수집 대상 추가. 출발지 PgBouncer는 별도 Deployment라 지표 exporter가 필요하다.
- **CI:** `promtool test rules`와 `testing/` 스크립트는 지금 CI에서 돌지 않는다(워크플로 변경 필요).
- **신뢰 레지스트리 제한:** trivy KSV-0125(중간)는 클러스터 승인 정책(Kyverno)에서 다룬다.
- **콘솔용 조회 API:** 정해 둔 조회식만 노출.
- **T34와 하나로:** 목적지 Collector를 감사 로그 파이프라인과 합칠지는 #117 머지 뒤 정한다.
