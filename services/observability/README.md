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

출발지 · 목적지 클러스터마다 OpenTelemetry Collector를 두고, 앱 · DB 지표를 플랫폼 지표 저장소(Prometheus)로 보낸다. 콘솔은 조회 API로 읽는다. 결정 배경은 [ADR-0010](../../docs/adr/0010-observability-boundary.md)이다. **판정 입력이 아니다.** SHIFT · WATCH 판정은 T18 창 요약만 쓴다([판정용 관측 계약](../../docs/observability/README.md)).

| 경로 | 내용 |
|---|---|
| [`deploy/collector/`](deploy/collector/) | 클러스터별 Collector: 설정 · 매니페스트 · NetworkPolicy |
| [`deploy/collector-db/`](deploy/collector-db/) | DB(CNPG)가 있는 클러스터용 오버레이: DB 지표 수집 추가 |
| [`deploy/collector-db-access/`](deploy/collector-db-access/) | DB 네임스페이스의 Pod 조회 권한(Role)만. 환경마다 namespace를 바꿔 적용 |
| [`deploy/platform/`](deploy/platform/) | 플랫폼: 지표 저장소(mTLS `web.yml`, 알림 `rules/`)와 콘솔용 조회 API |
| [`deploy/certs/issue.sh`](deploy/certs/issue.sh) | 인증서 발급 · 만료 검사. 절차: [인증서](../../docs/observability/certificates.md) |
| [`Dockerfile`](Dockerfile) | 조회 API 이미지(비루트, digest 고정 기반 이미지) |
| [`testing/`](testing/) | `run.sh`(compose), `k8s.sh`(Docker 안 k3s + CNPG), `rotation.sh`(인증서 교체) |
| [런북](../../docs/observability/runbooks.md) | 알림 8개의 뜻 · 확인 순서 · 조치 |

적용 순서(실제 환경): Secret · ConfigMap을 만들고(아래 "배포 전에 만들 것"), 플랫폼은 `kubectl apply -k deploy/platform`을 적용한다. DB가 있는 클러스터는 `deploy/collector-db`와 `deploy/collector-db-access`를, 없는 클러스터는 `deploy/collector`를 적용한다. 조회 API 이미지는 배포 파이프라인이 kustomize `images`로 레지스트리@digest를 넣는다. 넣지 않으면 pull이 실패한다.

### 동작

- **환경 이름은 Collector가 붙인다.** 앱이 보낸 `bravo.env`는 허용 목록에서 지워지고, Collector의 `BRAVO_ENV`로 덮어쓴다.
- **허용 목록:** 앱 지표는 아래 속성만 남는다. 채팅 본문 · 헤더 · 토큰 · SQL 파라미터는 저장되지 않는다.
  - 리소스: `service.name` · `service.instance.id`
  - 데이터 포인트: `http.request.method` · `http.response.status_code` · `http.route`
- **생존 신호:** Collector 자기 지표에만 신뢰 라벨 `bravo.signal=collector-self`를 붙인다. 성공한 `up=1` 원 표본이 30초 넘게 오지 않거나, 없거나, 미래 시각이면 그 환경은 "확인 불가"이고 `BravoEnvUnobservable` 알림이 뜬다.
- **DB 지표:** CNPG 인스턴스 Pod(`cnpg.io/podRole=instance`)의 `metrics` 포트를 15초마다 긁는다. 인스턴스 상태, 논리 복제 슬롯 지연 · 활성, WAL 보관 실패를 본다. 컷오버 조건의 복제 지연은 Executor가 DB에서 직접 읽는다(설계 8.6).
  - DB 긁기 경로에만 출처 라벨 `bravo.signal=cnpg-scrape`를 붙인다. 조회 API와 DB 알림은 이 라벨이 있는 지표만 쓰고, 조회 API는 CNPG 식별자(클러스터 · Pod)가 없는 행을 정상으로 올리지 않는다.
  - 앱 경로는 허용 목록이 이 라벨과 CNPG 식별자를 지운다. 앱이 같은 이름(`cnpg_collector_up` 등)이나 `service.name=bravo-cnpg`(`job` 라벨)로 보내도 DB 상태 · 알림을 바꾸지 못한다.
- **조회 API:** 정해 둔 조회식만 실행한다. 환경 이름은 등록 목록과 정확히 같을 때만 식에 들어간다. 저장소 장애·손상 응답·중복 단일 값은 503, 값이 없거나 NaN/Inf이면 null이다(0이 아님). 응답에는 `Cache-Control: no-store`를 넣는다.
  - HTTP·DB 뷰도 성공한 Collector 생존 신호를 먼저 확인한다. 신호가 오래되거나 없으면 과거 수치를 숨기고 `UNOBSERVABLE`로 응답한다.
  - DB 원 표본에도 30초 제한을 적용한다. Collector가 살아 있다는 이유만으로 오래된 DB 표본을 정상으로 표시하지 않는다.
  - `last_received_seconds_ago`는 성공한 **Collector 원 표본의 나이**이며 DB 트랜잭션/업무 응답 시각이 아니다. HTTP `state=OK`는 지표 조회 가능을 뜻하며 앱 건강·컷오버 허가가 아니다.
  - Prometheus JSON은 최대 2MiB·10,000행이다. 잘못된 숫자/구조를 응답에 반영하지 않으며 등록되지 않은 환경의 경보는 반환하지 않는다.
  - `GET /v1/observability/environments`: 환경별 OK · UNOBSERVABLE
  - `GET /v1/observability/alerts`: Bravo 알림과 런북
  - `GET /v1/observability/environments/{env}/http`: 요청률 · 오류율 · p95. OTel `http.server.request.duration` 기준
  - `GET /v1/observability/environments/{env}/database`: 인스턴스, 논리 슬롯
- **유실 내구성:** 보내지 못한 지표는 디스크 큐(5000건)에 쌓이고 10분 동안 재시도한다. 저장소가 늦은 표본을 받는 기간과 같다.
- **알림 8개(코드 · 단위 시험 · 런북)**
  - 관측 경로: 확인 불가, 전송 실패, 대기열, 거부
  - DB: 다운, 논리 슬롯 지연(64MiB, 제안값), 슬롯 비활성, WAL 보관 실패. 지표 이름은 k3s 실측으로 확인했다(counter는 `_total`).
- **보존:** 3일 또는 4GB. 판정의 근거는 판정 기록에 따로 남는다.

### 보안 경계

- **mTLS:** 지표 저장소는 쓰기 · 조회 모두 같은 CA가 발급하고 허용된 이름(`client_allowed_sans`)인 클라이언트 인증서만 받는다. TLS 1.3만 쓴다.
- **전송마다 새 연결:** 인증서 교체와 신뢰 제거가 다음 전송(10초 이내)에 반영된다. Collector 0.164.0은 기능 게이트 결함 때문에 `--feature-gates=-pkg.confighttp.PrioritizeNewKeepalive`가 필요하다([인증서 3절](../../docs/observability/certificates.md)).
- **남는 위험:** 인증서 이름은 환경 이름과 묶이지 않는다. 한 Collector 인증서가 유출되면 다른 환경 이름으로 서버 지표를 넣을 수 있다. 이 지표는 판정 입력이 아니다.
- **조회 API 인가:** Bearer 토큰(32자 이상, Secret). 설정이 없으면 시작하지 않는다.
- **NetworkPolicy**
  - Collector는 `bravo.observability/send=true` 라벨 네임스페이스에서만 받는다. 나가는 연결은 DNS와 저장소 포트, DB 오버레이면 API 서버와 9187.
  - 저장소는 같은 네임스페이스의 Collector · 조회 API, `platform-client` 라벨 네임스페이스, Tailscale 대역만 받는다. 나가는 연결은 없다.
  - 조회 API는 `platform-client` 라벨 네임스페이스에서만 받고, 저장소 9090으로만 나간다.
  - 라벨은 `kubectl label`로 붙인다(Namespace 매니페스트를 apply하면 없는 라벨이 지워진다).
- **DB 접근:** Collector는 DB 네임스페이스에서 Pod 조회만 할 수 있다(Secret · exec · 다른 네임스페이스는 거부, k3s에서 확인).
  - DB 네임스페이스에 Collector 허용 NetworkPolicy를 넣지 않는다. 그 정책이 DB Pod를 고르면 격리가 켜져 앱 → DB 연결까지 막힐 수 있다.
  - DB 네임스페이스가 이미 격리돼 있다면, 그 담당(T6 · T30)이 Collector → 9187을 허용한다.
- **Pod 보안:** 세 워크로드 모두 Pod 보안 표준 restricted를 만족한다(비루트, 읽기 전용 루트, 권한 상승 금지, capability 제거, seccomp). ServiceAccount 토큰은 DB 오버레이의 Collector만 붙인다(Pod 조회용).
- **배포 전에 만들 것(git에 두지 않음)**
  - Secret: `bravo-collector-tls`, `bravo-prometheus-tls`, `bravo-observability-api-tls`, `bravo-observability-api-token`
  - ConfigMap: `bravo-collector-env`(`BRAVO_ENV`, `PLATFORM_OTLP_ENDPOINT`, DB 오버레이면 `BRAVO_DB_NAMESPACE`)
- 리허설 네임스페이스에는 Collector로 가는 송신 예외를 열지 않는다(ADR-0002).

### 자원 (실측)

데모 예상의 약 10배 부하(앱 지표 2000건/초, 3분)에서 잰 값이다.

| 워크로드 | 최대 메모리 | 평균 / 최대 CPU | request / limit |
|---|---|---|---|
| Collector | 107MiB | 33m / 92m | 50m · 128Mi / 500m · 256Mi |
| Prometheus | 83MiB | 15m / 42m | 50m · 128Mi / 1 · 512Mi |
| 조회 API | 50MiB | 4m / 16m | 25m · 64Mi / 500m · 192Mi |

`BRAVO_K8S_CAPACITY=1 testing/k8s.sh`로 다시 잰다. 시계열 종류 수가 적은 부하였고 Docker Desktop VM이라, 실제 환경에서 다시 잰다.

### 검증 (2026-11-04 로컬)

결과 · 소스 해시 · 이미지 digest: [증빙](../../docs/evidence/t37-observability-2026-11-04/README.md).

```sh
services/observability/testing/run.sh        # compose: 20항목, 약 2분
services/observability/testing/k8s.sh        # k3s + CNPG: 16항목, 약 4분 (helm 필요)
services/observability/testing/rotation.sh   # 인증서 교체: 14항목, 약 1분 반
```

시험의 Compose 프로젝트·k3s 컨테이너·API 이미지·포트는 실행별로 분리한다. k3s 정리는 이번 실행이 생성한 컨테이너 ID에만 적용한다.

알림 단위 시험은 `promtool test rules deploy/platform/rules/alerts_test.yml`이다.

보호 장치를 하나씩 빼 보는 변이 시험으로, 시험이 실제로 결함을 잡는지 확인했다(증빙 `mutations_caught`). 대상은 허용 목록, 환경 덮어쓰기, 생존 신호 선택, 디스크 큐, 알림 조건 4종이다.

### 남은 일

- **실제 환경 배포:** 플랫폼 RKE2(T7), 출발지 EKS(T6), 목적지 RKE2(T30). 남은 것은 배포 경로, 클러스터 접근, 인증서 발급, 조회 API 이미지 게시다.
- **영속성:** 디스크 큐 · 저장소가 emptyDir이다. 저장소 클래스가 정해지면 PVC로 바꾼다.
- **나가는 주소 좁히기:** Collector의 나가는 연결은 포트로만 제한한다. 환경별 플랫폼 주소와 출발지 VPC 대역을 ipBlock으로 더한다.
- **앱 지표:** 데모 앱에 OTel 계측이 없다(T22). 조회 API의 HTTP 뷰는 계측이 들어오면 채워진다.
- **PgBouncer 지표:** 출발지 PgBouncer는 별도 Deployment라 exporter가 필요하다(T6).
- **CI:** `promtool`과 `testing/` 스크립트는 지금 CI에서 돌지 않는다(워크플로 변경 필요). pytest는 돈다.
- **신뢰 레지스트리 제한(KSV-0125):** 클러스터 승인 정책 몫이다.
- **T34와 하나로:** 목적지 Collector를 감사 로그 파이프라인과 합칠지는 #117 머지 뒤 정한다.
- **Collector 버전을 올리면** `testing/rotation.sh`로 keep-alive 게이트 결함이 고쳐졌는지 확인하고 게이트 인자를 뺀다.
