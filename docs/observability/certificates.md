# 관측 경로 인증서 (T37)

Collector → 지표 저장소, 콘솔 API → 지표 저장소는 모두 mTLS다. 발급 스크립트는 [`services/observability/deploy/certs/issue.sh`](../../services/observability/deploy/certs/issue.sh)이고, 시험(`testing/run.sh` · `k8s.sh` · `rotation.sh`)도 이 스크립트로 인증서를 만들어 스크립트 자체를 함께 검증한다.

## 1. 이름과 유효기간

| 인증서 | 이름(CN · SAN의 DNS) | 쓰는 곳 | 유효기간 |
|---|---|---|---|
| 관측 CA | `bravo-observability-ca-<날짜>-<무작위>` (매번 고유) | 모든 쪽의 `ca.crt` | 90일 |
| 출발지 Collector | `collector-aws` | Secret `bravo-collector-tls` (출발지 클러스터) | 30일 |
| 목적지 Collector | `collector-onprem` | Secret `bravo-collector-tls` (목적지 클러스터) | 30일 |
| 콘솔 API | `console-api` | Secret `bravo-observability-api-tls` | 30일 |
| 지표 저장소(서버) | `prometheus` + Collector가 부르는 주소 전부(서비스 DNS, 노드 주소) | Secret `bravo-prometheus-tls` | 30일 |

- 클라이언트 이름은 지표 저장소 `web.yml`의 `client_allowed_sans`에 있어야 한다. 발급 스크립트는 이름이 SAN에 없으면 발급하지 않는다.
- 환경 이름과 인증서 이름은 묶이지 않는다(README "남는 위험"). 인증서는 환경마다 따로 발급하고 다른 환경에 복사하지 않는다.
- leaf가 CA보다 오래 살 수 없다. 스크립트가 거부한다.
- 키는 EC P-256이다. leaf에는 발급자 키 식별자(AKID)가 들어가, 두 CA를 함께 믿는 교체 기간에도 체인을 헷갈리지 않는다.

## 2. 발급과 배포

```sh
I=services/observability/deploy/certs/issue.sh
$I ca  ./obs-ca 90                                                     # CA 키는 이 디렉터리 밖으로 내보내지 않는다
$I leaf ./obs-ca collector-onprem DNS:collector-onprem ./out/collector-onprem
$I leaf ./obs-ca prometheus "DNS:prometheus,DNS:bravo-prometheus.bravo-observability.svc,IP:<플랫폼 노드 Tailscale 주소>" ./out/prometheus
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
