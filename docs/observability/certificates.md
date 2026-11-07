# 관측 경로 인증서 (T37)

Collector → 지표 저장소, 콘솔 API → 지표 저장소는 모두 mTLS다. 발급 스크립트는 [`services/observability/deploy/certs/issue.sh`](../../services/observability/deploy/certs/issue.sh)이고, 시험(`testing/run.sh` · `k8s.sh` · `rotation.sh`)도 이 스크립트로 인증서를 만들어 스크립트 자체를 함께 검증한다.

## 1. 이름과 유효기간

| 인증서 | 이름(CN · SAN의 DNS) | 쓰는 곳 | 유효기간 |
|---|---|---|---|
| 관측 CA | `bravo-observability-ca-<날짜>-<무작위>` (매번 고유) | 모든 쪽의 `ca.crt` | 90일 |
| 플랫폼 수신 CA | 같은 규칙으로 따로 만든다 | 저장소 `ca.crt` 묶음에 추가. 플랫폼 Collector(`collector-platform`)만 발급 | 90일 |
| 출발지 Collector | `collector-aws` | Secret `bravo-collector-tls` (출발지 클러스터) | 30일 |
| 목적지 Collector | `collector-onprem` | Secret `bravo-collector-tls` (목적지 클러스터) | 30일 |
| 콘솔 API | `console-api` | Secret `bravo-observability-api-tls` | 30일 |
| 지표 저장소(서버) | `prometheus` + Collector가 부르는 주소 전부(서비스 DNS, tailnet 이름 `bravo-metrics.<tailnet>.ts.net`) | Secret `bravo-prometheus-tls` | 30일 |

- 클라이언트 이름은 지표 저장소 `web.yml`의 `client_allowed_sans`에 있어야 한다. 발급 스크립트는 이름이 SAN에 없으면 발급하지 않는다.
- 환경 이름과 인증서 이름은 묶이지 않는다(README "남는 위험"). 인증서는 환경마다 따로 발급하고 다른 환경에 복사하지 않는다.
- leaf가 CA보다 오래 살 수 없다. 스크립트가 거부한다.
- 키는 EC P-256이다. leaf에는 발급자 키 식별자(AKID)가 들어가, 두 CA를 함께 믿는 교체 기간에도 체인을 헷갈리지 않는다.

## 2. 발급과 배포

```sh
I=services/observability/deploy/certs/issue.sh
$I ca  ./obs-ca 90                                                     # CA 키는 이 디렉터리 밖으로 내보내지 않는다
$I leaf ./obs-ca collector-onprem DNS:collector-onprem ./out/collector-onprem
$I leaf ./obs-ca prometheus "DNS:prometheus,DNS:bravo-prometheus.bravo-observability.svc,DNS:bravo-metrics.<tailnet>.ts.net" ./out/prometheus
kubectl -n bravo-observability create secret generic bravo-collector-tls \
  --from-file=./out/collector-onprem/ca.crt --from-file=./out/collector-onprem/tls.crt --from-file=./out/collector-onprem/tls.key
```

- **CA 키 보관:** 지금은 발급자 PC의 디렉터리에 둔다. 클러스터 Secret · git · 공유 드라이브에 두지 않는다. 목적지 OpenBao(T9)가 준비되면 PKI 엔진으로 옮기는 것을 제안한다.
- **출력 키:** 0600이다. 클러스터 Secret은 0440으로 마운트하고, Pod의 `fsGroup`이 읽는다. 조회 API Pod에 `fsGroup`이 없어 실제로 읽지 못한 결함을 k3s 시험에서 찾아 고쳤다.

## 3. 교체

`testing/rotation.sh`가 아래 절차를 그대로 실행해 확인한다(2026-11-04, 14/14 통과).

### leaf 교체 (재시작 없음)

1. 같은 CA로 새 leaf를 발급한다.
2. Secret을 갱신한다. kubelet이 마운트된 파일을 갱신한다.
3. 반영 시점은 쪽마다 다르다.
   - **Collector 클라이언트 인증서:** `reload_interval`(기본 1분) 안에 새 파일을 읽는다. 전송마다 새 연결을 맺으므로 다음 전송부터 새 인증서를 낸다.
   - **지표 저장소 서버 인증서:** 연결마다 파일을 다시 읽는다(Prometheus exporter-toolkit). 다음 연결부터 새 인증서다.
   - **콘솔 API:** 시작할 때 읽는다. `kubectl rollout restart deploy/bravo-observability-api`.

### CA 교체 (옛 · 새 CA를 함께 믿는 기간을 거친다)

1. **(a) 묶음 배포:** 모든 쪽의 `ca.crt`를 "옛 CA + 새 CA" 묶음으로 바꾼다.
   - 지표 저장소는 재시작이 필요 없다.
   - **Collector와 콘솔 API는 재시작해야 한다.** Collector는 서버를 검증하는 CA 목록(RootCAs)을 시작할 때만 읽는다(configtls). 클라이언트 인증서와 달리 이 목록은 `reload_interval`로 다시 읽지 않는다.
2. **(b) 재발급:** 새 CA로 모든 leaf를 다시 발급해 교체한다(위 leaf 교체 절차).
3. **(c) 옛 CA 제거:** 지표 저장소의 `ca.crt`에서 옛 CA를 뺀다. 그때부터 옛 CA 인증서는 거부된다. Collector 묶음에서 옛 CA를 빼는 것은 다음 재시작 때 반영된다.

### 전송마다 새 연결을 맺는 이유와 0.164.0 주의

클라이언트 인증서는 TLS 핸드셰이크 때만 확인된다. 연결을 계속 쓰면 교체한 인증서도, 거둔 신뢰(CA 제거 · 허용 이름 제거)도 그 연결에는 반영되지 않는다. 그래서 Collector는 `disable_keep_alives: true`, `force_attempt_http2: false`로 전송마다 새 연결을 맺는다. 비용은 배치당 핸드셰이크 한 번이다.

Collector 0.164.0은 기능 게이트 `pkg.confighttp.PrioritizeNewKeepalive`(Beta, 기본 켜짐) 경로에서 `disable_keep_alives`를 무시한다. `config/confighttp/client.go` v0.164.0에서 Unmarshal 끝에 `DisableKeepAlives = false`가 되고, `ToClient`가 그 값을 transport에 넣는다. 그래서 배포는 `--feature-gates=-pkg.confighttp.PrioritizeNewKeepalive`로 게이트를 끈다.

이 사실은 교체 시험이 실제로 찾았다.
- 인증서를 다른 CA 것으로 바꿔도 전송이 계속 성공했다.
- 지표 저장소를 재시작해 연결을 끊자 그제야 거부됐다.

Collector 버전을 올리면 `testing/rotation.sh`로 다시 확인하고, 고쳐진 버전이면 게이트 인자를 뺀다.

## 4. 만료 감시

```sh
services/observability/deploy/certs/issue.sh check ./out/collector-onprem/tls.crt 10   # 10일 안에 만료면 실패(exit 1)
```

- leaf는 수명의 3분의 2(30일 인증서면 20일째)에 교체한다.
- 지표 저장소는 클라이언트 인증서 만료를 지표로 내지 않는다. 그래서 만료 알림 대신, 교체 일정과 위 검사를 발급자가 돌린다.
- 인증서가 만료되면 그 환경은 `BravoEnvUnobservable`로 드러난다(전송 거부 → 생존 신호 끊김).

## 5. 앱 → Collector mTLS

원격 앱은 `deploy/collector-app-mtls` 또는 `components/app-mtls`를 사용한다.
기본 `collector`의 평문 OTLP 수신 구성은 기존 내부 시험과의 호환용이며,
T22 앱은 loopback 이외의 평문 주소를 거부한다. NetworkPolicy만으로 구간 암호화를 대신하지 않는다.

- `bravo-collector-app-tls` Secret: 서버 `tls.crt`·`tls.key`, 허용 앱 발급 CA `client-ca.crt`.
- 서버 SAN에는 앱이 접속하는 Service DNS를 포함한다.
- 앱에는 서버 CA와 앱 전용 클라이언트 인증서/키를 별도로 전달한다.
  `BRAVO_METRICS_CA_FILE`, `BRAVO_METRICS_CLIENT_CERT`, `BRAVO_METRICS_CLIENT_KEY`를 사용한다.
- 앱 수신 CA와 플랫폼 전송 CA를 분리한다. 플랫폼 Collector/조회 API 인증서를 앱에 배포하지 않는다.
- HTTP·gRPC 모두 TLS 1.3 이상, 클라이언트 인증서 필수다. Secret이 없거나 잘못되면 시작하지 않는다.
- DB 수집이 필요하면 환경 overlay의 `resources`를 `collector-db`, `components`를
  `components/app-mtls`로 구성한다. DB scrape 권한과 앱 TLS는 별도 기능이다.

이 구성은 발급 CA를 신뢰하는 mTLS다. 임의의 인증서 SAN을 앱별 권한으로 매핑하는
인가 기능은 제공하지 않는다. 발급 권한·앱별 Secret 접근·기존 NetworkPolicy를 함께 제한한다.
앱 계측값은 T36 판정 근거가 아니라 표시용이다.

서버 인증서는 1분 reload, 클라이언트 CA는 파일 변경 감지를 사용한다.
기존 TLS 연결을 즉시 종료한다는 보장은 없다. 긴급 신뢰 회수 시 Collector와 앱을
재시작해 연결을 끊고, 거부 대조군과 정상 전송을 다시 확인한다.
CA 자동 교체/폐기 전체 운영 절차는 별도 자격 시험 대상이다.

### 앱 인증서 발급 (출발지 demo-chat, #570의 전제)

앱 쪽은 관측 CA와 **다른** 앱 CA 하나로 서버·클라이언트를 모두 발급한다. 앱은 이 CA로 Collector를 검증하고,
Collector는 같은 CA를 `client-ca.crt`로 믿어 앱을 검증한다.

| 인증서 | 이름(CN · SAN의 DNS) | Secret (네임스페이스) | 유효기간 |
|---|---|---|---|
| 앱 CA | `bravo-observability-ca-<날짜>-<무작위>` (관측 CA와 따로 만든다) | 아래 두 Secret의 `ca.crt` · `client-ca.crt` | 90일 |
| Collector 앱 수신(서버) | `bravo-collector` + 앱이 부르는 Service DNS | `bravo-collector-app-tls` (`bravo-observability`) | 30일 |
| 출발지 demo-chat(클라이언트) | `demo-chat-source` | `bravo-app-metrics-client` (`source-dev`) | 30일 |

**먼저 이미 배포됐는지 본다.** 출발지 EKS Collector는 2026-11-06에 `collector-app-mtls`로 앱 수신(별도 앱 CA,
TLS 1.3 mTLS)까지 배포됐다([증빙](../evidence/t37-source-collector-2026-11-06/README.md)). 그 CA 개인 키는 클러스터
밖에서 관측 담당이 보관한다. 이때 아래 "새로 만드는 경우"를 그대로 실행하면 `bravo-collector-app-tls`가
`AlreadyExists`로 멈춘다. 지우고 다시 만들면 Collector가 믿는 CA가 바뀌어 그 CA로 발급한 기존 클라이언트가 거부된다.

```sh
# 이미 배포된 경우: Collector가 믿는 CA와 서버 인증서 발급자를 읽기만 한다(키는 클러스터에 없다)
kubectl -n bravo-observability get secret bravo-collector-app-tls -o jsonpath='{.data.client-ca\.crt}' \
  | base64 -d | openssl x509 -noout -subject -enddate -fingerprint -sha256
kubectl -n bravo-observability get secret bravo-collector-app-tls -o jsonpath='{.data.tls\.crt}' \
  | base64 -d | openssl x509 -noout -issuer -enddate
openssl x509 -in ./app-ca/ca.crt -noout -subject -fingerprint -sha256   # 보관 중인 앱 CA와 같아야 한다

# 같으면: 그 CA로 앱 클라이언트 인증서만 발급하고 앱 Secret만 만든다. Collector Secret은 건드리지 않는다
I=services/observability/deploy/certs/issue.sh
$I leaf ./app-ca demo-chat-source DNS:demo-chat-source ./out/demo-chat-source
kubectl -n source-dev create secret generic bravo-app-metrics-client \
  --from-file=./out/demo-chat-source/ca.crt --from-file=./out/demo-chat-source/tls.crt \
  --from-file=./out/demo-chat-source/tls.key
```

- 지문이 다르거나 보관 중인 앱 CA가 없으면 발급하지 않는다. CA를 바꿔야 하면 3절 교체 절차를 따른다:
  `client-ca.crt`와 앱의 `ca.crt`에 옛·새 CA를 함께 넣고, 새 CA leaf로 바꾼 뒤 옛 CA를 뺀다.
- 서버 인증서 발급자(`tls.crt`의 issuer)가 `client-ca.crt`의 subject와 다르면 앱이 Collector를 믿지 못한다.
  앱 `ca.crt`에는 서버 인증서를 발급한 CA를 넣는다.

**새로 만드는 경우**(그 클러스터에 `bravo-collector-app-tls`가 없을 때만):

```sh
I=services/observability/deploy/certs/issue.sh
$I ca   ./app-ca 90                     # 관측 CA(./obs-ca)와 다른 디렉터리. CA 키는 밖으로 내보내지 않는다
$I leaf ./app-ca bravo-collector \
  "DNS:bravo-collector,DNS:bravo-collector.bravo-observability.svc,DNS:bravo-collector.bravo-observability.svc.cluster.local" \
  ./out/collector-app
$I leaf ./app-ca demo-chat-source DNS:demo-chat-source ./out/demo-chat-source

# Collector (출발지 클러스터): 서버 인증서와, 앱을 믿을 CA
kubectl -n bravo-observability create secret generic bravo-collector-app-tls \
  --from-file=./out/collector-app/tls.crt --from-file=./out/collector-app/tls.key \
  --from-file=client-ca.crt=./app-ca/ca.crt
# 앱 (출발지 클러스터): Collector를 믿을 CA와 앱 전용 클라이언트 인증서
kubectl -n source-dev create secret generic bravo-app-metrics-client \
  --from-file=./out/demo-chat-source/ca.crt --from-file=./out/demo-chat-source/tls.crt \
  --from-file=./out/demo-chat-source/tls.key
```

- **순서:** 두 Secret을 먼저 만들고 Collector에 `collector-app-mtls`(또는 `components/app-mtls`)를 적용한 뒤, 앱 렌더(#570)를 적용한다. Secret이 없으면 Collector와 앱 모두 시작하지 않는다.
- **이름:** 앱 엔드포인트는 `https://bravo-collector.bravo-observability.svc:4318/v1/metrics`다. 서버 SAN에 이 이름이 없으면 앱이 연결을 거부한다.
- **분리:** 앱 CA로 관측 경로(Collector → 지표 저장소) 인증서를 발급하지 않는다. 반대로 관측 CA 인증서를 앱에 주지 않는다.
- **읽기 권한:** 앱 Secret은 0440(root 소유)으로 마운트된다. 지금 앱은 이미지의 기본 그룹(0)으로 읽는다. Pod에 `runAsGroup`을 넣거나 이미지 사용자를 바꾸면 `fsGroup`도 함께 지정한다(#570 리뷰).
- **교체:** leaf 교체 · CA 교체는 3절 절차와 같다. 앱 CA 교체 중에는 `client-ca.crt`와 앱의 `ca.crt`에 옛 · 새 CA를 함께 넣는다.
- **로컬 확인(2026-11-07, 플랫폼 · 실클러스터 아님):** 위 이름 · 확장값으로 발급한 인증서를 Collector 수신 설정과 같은 조건(TLS 1.3, 클라이언트 인증서 필수 · `client-ca.crt` 검증)의 서버에 붙여 보았다. 앱 인증서 + Service DNS · FQDN은 연결되고, 클라이언트 인증서 없음 · 관측 CA 등 다른 CA의 인증서 · 다른 서버 이름은 모두 거부됐다.
- **Windows Git Bash:** `issue.sh`의 `-subj`(앞의 `/`)와 `-extfile <(...)`가 동작하지 않는다. Linux · macOS 셸에서 발급한다.
- **목적지(`target-dev`, #571):** 목적지 클러스터 Collector에도 위와 같이 먼저 `bravo-collector-app-tls`가 있는지 본다.
  있으면 그 앱 CA로, 없으면 "새로 만드는 경우"로 목적지 Collector 수신 Secret을 만든다. 클라이언트는 `demo-chat-target`
  (CN · SAN `DNS:demo-chat-target`)으로 발급해 `target-dev`에 `bravo-app-metrics-client`를 만든다. 발급은 앱 CA 개인 키
  보관자(관측 담당)가 한다. 이 Secret이 #571 적용 전 조건 0번이다.

## 6. 원격 콘솔 → 조회 API TLS

`services/observability/deploy/platform-api-tls`는 조회 API에 서버 TLS를 추가한다.
지표 저장소 접속에 쓰는 `bravo-observability-api-tls`(console-api 클라이언트)와
별도로 `bravo-observability-api-server-tls`를 발급한다. 서버 인증서를 발급할 CA는
콘솔 호스트가 신뢰해야 하며 CA 개인 키를 배포하지 않는다.

- Secret 키: `tls.crt`, `tls.key`, `ca.crt`. 인증서 SAN은 실제 API 접속 DNS와 일치해야 한다.
- 기본 Service DNS: `bravo-observability-api.bravo-observability.svc`. 별도 인그레스 주소를
  쓰면 인증서와 콘솔 설정도 함께 등록한다. 임의 브라우저 입력 URL은 허용하지 않는다.
- TLS 1.3 이상을 사용한다. TLS 서버 설정이 있지만 인증서가 없거나 키가 맞지 않으면
  시작하지 않는다. HTTP로 자동 복귀하지 않는다.
- API 서버 TLS는 클라이언트 인증서를 요구하는 mTLS가 아니다. TLS 서버 신원 확인 뒤
  기존 Bearer 인가와 NetworkPolicy를 적용한다. 저장소 방향의 mTLS는 그대로 유지한다.
- 서버 인증서와 신뢰 변경은 API/콘솔 재시작이 필요하다. Secret 갱신만으로 즉시 교체됐다고
  간주하지 않는다. 정상 조회·잘못된 CA/서버 이름 거부·잘못된 토큰 거부를 다시 확인한다.
- Docker healthcheck는 loopback에 연결하되 설정된 서버 이름과 CA를 검증한다.
  Kubernetes의 HTTPS probe는 인증서 신뢰를 검사하지 않으므로 별도 TLS 인수 시험이 필요하다.

`tests/test_api_server_tls.py`는 임시 인증서와 실제 로컬 소켓에서 TLS 거부와 인증을 시험한다.
저장소 응답은 빈 모의 값이므로 운영 수집이나 양쪽 환경의 지표 인수를 증명하지 않는다.

필드 근거: [Collector v0.164.0 configtls ServerConfig](https://github.com/open-telemetry/opentelemetry-collector/blob/v0.164.0/config/configtls/generated_config.go).
