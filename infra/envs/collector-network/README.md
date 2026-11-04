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
| `proxy-quarantine --side S` | 아직 없는 전용 proxy Pod까지 선택하는 ingress 기본 차단. 경로/ACL 생성 전에 적용·서버 readback |
| `routes --side S --metrics-fqdn F --policy-readback R` | 클러스터 범위 ProxyGroup `bravo-S-collector`(egress, 1개, 태그 `tag:bravo-S-collector`)와 `bravo-observability/bravo-metrics` ExternalName Service(TCP 9090만) |
| `collector-egress --side S` | `bravo-collector-tailnet` NetworkPolicy: Collector Pod → `tailscale-system`의 이 ProxyGroup Pod만, 9090과 관찰한 재매핑 포트만 |
| `proxy-ingress --side S --transport-port P` | proxy Pod 수신 제한: Collector에서 오는 업무 포트, operator 상태 확인 9002, WireGuard UDP |
| [`tailnet-fragment.json`](tailnet-fragment.json) | 기존 tailnet 정책에 **합칠 추가분**. 태그 소유, 두 Collector 태그 → `tag:bravo-metrics` TCP 9090 grant, 허용 2 · 거부 시험 16 |

Collector 기본 정책([`resources.yaml`](../../../services/observability/deploy/collector/resources.yaml))에서 "어느 목적지든 9090" 규칙을 없앴다. 같은 클러스터의 `app: bravo-prometheus` Pod(시험 · 플랫폼 배치)로만 9090이 열리고, 다른 클러스터의 저장소로는 이 디렉터리의 `bravo-collector-tailnet` 정책으로만 나간다.

## 순서: 먼저 닫고, 관찰한 업무 포트만 연다

NetworkPolicy는 **합집합**이다. 기본 차단 정책이 있어도 다른 정책이 같은 Pod를 허용하면 열린다. `routes`는 전용 proxy를 선택하는 기본 차단의 실제 서버 readback과 다른 ingress 정책 목록을 요구한다. 빈 목록·페이지 누락·삭제 중인 정책·다른 허용 정책과의 잠재 중첩은 거부한다. 이 검사는 렌더링 전제이며 적용 시점의 CNI 집행이나 이후 변경을 보증하지 않는다.

1. **기본 차단을 가장 먼저 적용(T30 · 출발지 담당).** 신규 ProxyGroup/Service 또는 tailnet grant를 만들기 전에 실행한다. 기존 경로를 변경한다면 담당자가 먼저 수집 중단·영향 범위를 승인해야 한다.

   ```sh
   python3 infra/envs/collector-network/collector_route.py proxy-quarantine --side source > quarantine.json
   kubectl apply --server-side --dry-run=server -f quarantine.json
   kubectl apply --server-side -f quarantine.json
   kubectl -n tailscale-system get networkpolicy -o json > policies-before-route.json
   ```

   서버 readback의 정책 UID/resourceVersion·선택 라벨·빈 ingress와 다른 정책의 중첩을 확인한다. 전체 namespace 정책 목록을 넘긴다. 별도 허용 정책을 지워 검사를 통과시키지 말고 그 담당자와 범위를 합의한다.

2. **경로 렌더링.** 저장소의 MagicDNS 이름을 확인한 뒤 적용한다. 이 단계에서 proxy가 기동돼도 기본 차단 상태다. 잘못된 관찰·렌더링 실패가 나면 차단을 유지한다.

   ```sh
   python3 infra/envs/collector-network/collector_route.py routes --side source \
     --metrics-fqdn "$VERIFIED_METRICS_FQDN" --policy-readback policies-before-route.json > route.json
   kubectl apply --server-side --dry-run=server -f route.json
   kubectl apply --server-side -f route.json
   ```

3. **관찰한 포트로만 제한 해제.** operator의 child Service UID/포트와 ProxyGroup Pod UID/UDP listen port를 읽는다. 기존 기본 차단과 **같은 이름**의 정책을 좁은 허용 정책으로 갱신한다. 교체 전에 기본 차단을 삭제하지 않는다.

   ```sh
   kubectl -n tailscale-system get svc -l tailscale.com/proxy-group=bravo-source-collector -o json > observed.json
   python3 infra/envs/collector-network/collector_route.py collector-egress --side source < observed.json > egress.json
   python3 infra/envs/collector-network/collector_route.py proxy-ingress --side source \
     --transport-port "$OBSERVED_UDP_PORT" < observed.json > proxy-ingress.json
   kubectl apply --server-side --dry-run=server -f egress.json -f proxy-ingress.json
   kubectl apply --server-side -f egress.json -f proxy-ingress.json
   ```

   두 렌더링이 모두 성공한 뒤 적용한다. 적용·readback 실패 시 quarantine을 다시 적용하고 실패 상태로 남긴다. UDP 관찰이나 상태 확인이 기본 차단 때문에 불가능하면 Pod 내부의 승인된 조회를 사용하며 전체 ingress를 열지 않는다. 다른 namespace·같은 namespace의 잘못된 앱 라벨에서 TCP 연결이 안 되고 Collector만 연결되는지 시험한다. proxy Pod/child Service가 바뀌면 기본 차단부터 다시 검증한다.

4. **tailnet 정책(T7).** 위 수신 제한을 확인한 후 현재 정책에 `tailnet-fragment.json`을 합친다. 파일로 전체 정책을 교체하지 않는다. Tailscale의 허용/거부 tests를 통과해야 하며 실제 다른 태그의 연결 거부도 확인한다.

5. **Collector 설정과 새 프로세스 확인.** 아래 갱신 절차를 수행한다. ConfigMap 수정만으로 연결 성공을 주장하지 않는다.

## Collector 설정 갱신과 인수

`configMapKeyRef` 환경변수는 실행 중인 Collector에 다시 주입되지 않는다. TLS 인증서 파일의 reload와도 다른 동작이다. `Ready`는 exporter가 대상 저장소에 전송했다는 증거가 아니다.

1. 기존 Pod UID·Deployment revision·현재 ConfigMap(비밀 제외)·저장소의 해당 환경 최근 수신 시각을 기록한다.
2. **큐와 관측 공백을 먼저 처리한다.** 현재 queue는 `emptyDir`이므로 Pod 교체 시 대기 지표를 잃는다. 유입 제어·기존 경로 drain을 담당자와 합의하고 배치/재시도 중인 전송과 큐 잔량을 확인한다. 잔량을 비울 수 없거나 관측 공백 승인이 없다면 교체를 보류한다. 임의 큐 삭제나 단순 `rollout restart`를 해결책으로 사용하지 않는다. 연속 보존이 필요한 운영 환경은 PVC 등 보존 경로를 별도 설계·검증해야 한다.
3. 기존 ConfigMap의 DB/Pooler 등 다른 키를 보존해 **새 이름의 immutable ConfigMap**을 만든다. `PLATFORM_OTLP_ENDPOINT=https://bravo-metrics.bravo-observability.svc:9090/api/v1/otlp`, `PLATFORM_TLS_SERVER_NAME=<검증한 저장소 MagicDNS 이름>`을 넣는다. ConfigMap 데이터의 hash와 이름을 기록한다.
4. 승인된 배포 변경에서 Collector의 모든 `configMapKeyRef`를 그 새 이름으로 바꾼다. `Recreate`에 따른 관측 공백을 기록하고 새 Pod Ready를 기다린다. 변경 전과 **다른 Pod UID**, 원하는 이미지 digest, 새 ConfigMap 참조와 immutable 데이터 hash를 readback한다. 이전 Pod가 계속 실행 중이거나 참조가 다르면 미완료다.
5. 새 Pod 생성 이후의 표본이 **실제 플랫폼 저장소**에 들어오고, 동일 환경의 `collector-self` 최근 수신 시각이 두 번 이상 앞으로 갔는지 확인한다. 이전 Pod가 마지막으로 전송한 표본·단순 Pod Ready·ConfigMap 변경만으로 성공 처리하지 않는다. 대상의 mTLS 인증과 양쪽 환경 라벨도 확인한다.
6. 격리된 시험에서는 TLS 이름을 일부러 틀리게 한 새 immutable ConfigMap/Pod로 대조한다. Ready여도 x509 검증 실패와 수신 미갱신이면 연결 실패다. 올바른 설정으로 다시 교체한 뒤 새로운 수신이 확인돼야 복구 성공이다. 실제 운영 저장소에 이 음성 시험을 임의로 주입하지 않는다.

인수 기록은 이전/신규 Pod UID·이미지·immutable 설정 hash·변경 시각·실제 수신 시각·거부 시험을 함께 묶는다. ConfigMap 교체 뒤 기존 큐가 사라진 시험을 무손실 갱신으로 표시하지 않는다.

## 검증 (로컬)

| 시험 | 결과 |
|---|---|
| `scripts/tests/test_collector_route.py` | 15개 통과. 기본 차단 readback과 중첩 허용·누락·삭제 상태 거부 추가. 쪽마다 전용 태그, MagicDNS 이름 검사, 관찰 포트만 허용 · 다른 쪽/바뀐 매핑 거부, proxy 수신 제한, ACL 조각(grant 하나, 저장소에서 찾은 다른 모든 `tag:bravo-*`의 거부 시험), Collector 9090 규칙에 목적지가 있는지 |
| 기본 차단 → 제한 허용(k3s 1.35.9, 11/4) | proxy 라벨의 시험 Pod 생성 전 기본 차단 적용. Collector/잘못된 라벨/다른 namespace 모두 TCP 거부. 잘못된 포트 관찰 후 차단 유지. 10421 제한 허용 뒤 Collector만 연결되고 나머지 두 경로 거부. operator child-port 입력은 합성 fixture이며 실제 Tailscale 검증 아님 |
| Collector 0.164.0 TLS 이름 A/B(Docker) | `PLATFORM_TLS_SERVER_NAME` 지정: egress Service 이름으로 접속해도 저장소가 OTLP 6건 수신, x509 오류 0. 미지정(빈 기본값): `x509: certificate is valid for bravo-metrics…, not bravo-metrics.bravo-observability.svc`로 거부 |

**재지 않은 것:** 실제 Tailscale operator · ProxyGroup · ACL 저장 시험. k3s에는 operator가 없다. 실제 tailnet 경로는 T7 · T30 적용 뒤 위 5단계로 확인한다.
