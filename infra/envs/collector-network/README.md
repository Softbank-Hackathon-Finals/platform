# Collector → 플랫폼 지표 저장소 tailnet 경로 (T37)

출발지 · 목적지 Collector가 플랫폼 지표 저장소(`bravo-metrics`, [tailnet 노출](../../../services/observability/README.md#tailnet-노출-다른-클러스터의-collector--지표-저장소))로 가는 경로를 렌더링한다. 노드 · ACL · operator를 바꾸지 않는다. 적용은 각 클러스터와 tailnet 담당(T7 · T30)이 한다.

## 왜 Collector 전용 egress인가

| 방법 | tailnet이 보는 출발지 | 판단 |
|---|---|---|
| 노드의 호스트 Tailscale(온프렘 노드) | 노드 태그. 그 노드의 모든 Pod가 같은 신원 | 쓰지 않는다. ACL로 Collector만 골라낼 수 없다 |
| 공용 egress ProxyGroup(다른 용도와 공유) | 공유 태그 | 쓰지 않는다. 복제 · 컷오버 경로와 권한이 섞인다 |
| **Collector 전용 egress ProxyGroup** | `tag:bravo-source-collector` / `tag:bravo-target-collector` | **사용.** ACL이 이 두 태그에서 `tag:bravo-metrics:9090`만 허용한다 |

T30 [`network.py`](../target-workloads/network.py)의 역할별 ProxyGroup + ExternalName 방식과 같고, 관찰한 포트로 NetworkPolicy를 만드는 부분은 T12 [`control_network.py`](../executor-network/control_network.py)의 `observed_proxy_ports`를 그대로 쓴다.

## 만드는 것

| 단계 | 결과 |
|---|---|
| `routes --side S --metrics-fqdn F` | 클러스터 범위 ProxyGroup `bravo-S-collector`(egress, 1개, 태그 `tag:bravo-S-collector`)와 `bravo-observability/bravo-metrics` ExternalName Service(TCP 9090만) |
| `collector-egress --side S` | `bravo-collector-tailnet` NetworkPolicy: Collector Pod → `tailscale-system`의 이 ProxyGroup Pod만, 9090과 관찰한 재매핑 포트만 |
| `proxy-ingress --side S --transport-port P` | proxy Pod 수신 제한: Collector에서 오는 업무 포트, operator 상태 확인 9002, WireGuard UDP |
| [`tailnet-fragment.json`](tailnet-fragment.json) | 기존 tailnet 정책에 **합칠 추가분**. 태그 소유, 두 Collector 태그 → `tag:bravo-metrics` TCP 9090 grant, 허용 2 · 거부 시험 16 |

Collector 기본 정책([`resources.yaml`](../../../services/observability/deploy/collector/resources.yaml))에서 "어느 목적지든 9090" 규칙을 없앴다. 같은 클러스터의 `app: bravo-prometheus` Pod(시험 · 플랫폼 배치)로만 9090이 열리고, 다른 클러스터의 저장소로는 이 디렉터리의 `bravo-collector-tailnet` 정책으로만 나간다.

## 순서

1. **tailnet 정책(T7):** 현재 정책을 다시 읽고 `tailnet-fragment.json`을 합친다. 전체 정책을 이 파일로 바꾸지 않는다. Tailscale은 저장할 때 `tests`를 실행하므로, 거부 시험이 하나라도 뚫리면 저장이 거부된다.
2. **경로 렌더링(T30 · 출발지 담당):** 저장소의 MagicDNS 이름을 확인한 뒤

   ```sh
   python3 infra/envs/collector-network/collector_route.py routes --side source \
     --metrics-fqdn "$VERIFIED_METRICS_FQDN" > source-collector-route.json
   kubectl apply --server-side --dry-run=server -f source-collector-route.json
   kubectl apply --server-side -f source-collector-route.json
   ```

3. **관찰한 포트로 정책:** operator가 만든 Service를 읽어 정책을 만든다. 포트는 operator가 `[10000, 11000)`에서 고르므로 Pod를 다시 만들면 다시 읽는다.

   ```sh
   kubectl -n tailscale-system get svc -l tailscale.com/proxy-group=bravo-source-collector -o json > observed.json
   python3 infra/envs/collector-network/collector_route.py collector-egress --side source < observed.json > egress.json
   python3 infra/envs/collector-network/collector_route.py proxy-ingress --side source \
     --transport-port "$OBSERVED_UDP_PORT" < observed.json > proxy-ingress.json
   ```

4. **Collector 설정:** `bravo-collector-env`에 `PLATFORM_OTLP_ENDPOINT=https://bravo-metrics.bravo-observability.svc:9090/api/v1/otlp`, `PLATFORM_TLS_SERVER_NAME=<저장소 MagicDNS 이름>`. Collector는 접속 주소와 다른 이 이름으로 저장소 인증서를 검증한다(mTLS 그대로).
5. **확인:** 플랫폼 조회 API에서 그 환경의 최근 수신 시각이 갱신되는지(양성), 다른 네임스페이스 Pod가 proxy 9090에 닿지 않는지 · 다른 태그가 `bravo-metrics:9090`에 닿지 않는지(음성). 렌더링 · Ready · 정책 읽기는 연결 시험이 아니다.

## 검증 (로컬)

| 시험 | 결과 |
|---|---|
| `scripts/tests/test_collector_route.py` | 10개 통과. 쪽마다 전용 태그, MagicDNS 이름 검사, 관찰 포트만 허용 · 다른 쪽/바뀐 매핑 거부, proxy 수신 제한, ACL 조각(grant 하나, 저장소에서 찾은 다른 모든 `tag:bravo-*`의 거부 시험), Collector 9090 규칙에 목적지가 있는지 |
| Collector 0.164.0 TLS 이름 A/B(Docker) | `PLATFORM_TLS_SERVER_NAME` 지정: egress Service 이름으로 접속해도 저장소가 OTLP 6건 수신, x509 오류 0. 미지정(빈 기본값): `x509: certificate is valid for bravo-metrics…, not bravo-metrics.bravo-observability.svc`로 거부 |

**재지 않은 것:** 실제 Tailscale operator · ProxyGroup · ACL 저장 시험. k3s에는 operator가 없다. 실제 tailnet 경로는 T7 · T30 적용 뒤 위 5단계로 확인한다.
