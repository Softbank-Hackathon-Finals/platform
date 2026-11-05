# 기존 Edge Tunnel을 앱에 연결하기

`render.py`는 기존 Cloudflare Tunnel을 연결할 `cloudflared` Deployment를 만든다.
출발지와 목적지 앱 네임스페이스에서 각각 실행한다. LB, pool, Tunnel, DNS 또는
토큰을 새로 만들지 않는다. 가중치 실행은 기존 [T17 어댑터](../../../services/edge-control/README.md)를 사용한다.

## 배포 조건

1. 현재 클러스터와 네임스페이스의 신원을 확인한다. 앱 Service `demo-chat:8080`과
   `/readyz`, `/api/health/db`, 환경별 `X-Bravo-Env`를 확인한다.
2. 해당 환경의 기존 Tunnel 토큰을 승인된 관리 세션에서 직접 Secret
   `bravo-edge-tunnel-token`의 `token` 키로 전달한다. 토큰은 파일, 명령 인자,
   SSM Run Command, Git 또는 로그에 남기지 않는다. 다른 환경의 토큰을 재사용하지 않는다.
3. 기존 Tunnel의 원격 ingress를 해당 앱 Service로 지정한다. 출발지는
   `http://demo-chat.source-dev.svc.cluster.local:8080`, 목적지는
   `http://demo-chat.target-dev.svc.cluster.local:8080`이다. 마지막 규칙은
   `http_status:404`로 유지한다. pool의 Host 헤더는 해당 Tunnel의 호스트와 같아야 한다.
4. 명시한 클러스터에 Deployment를 적용한다.

```sh
python3 render.py source-dev | kubectl --context <approved-source-context> apply -f -
python3 render.py target-dev | kubectl --context <approved-target-context> apply -f -
```

5. Pod Ready, provider 연결, pool health, 공개 앱 경로를 순서대로 확인한다.
   LB를 활성화하기 전 출발지 100%·목적지 0%와 출발지 fallback을 확인한다.

`cloudflared`는 버전과 multi-platform digest로 고정했다. 토큰은 읽기 전용
Secret 파일에서 읽으며, Kubernetes API 토큰을 mount하지 않는다. 비루트 UID,
읽기 전용 root, 모든 capability 제거와 CPU/메모리 제한을 적용한다. 연결은
HTTP/2를 사용하며 Cloudflare로 나가는 TCP 7844와 클러스터 DNS·앱 통신이 필요하다.
metrics의 `/ready`를 readiness와 liveness에 사용한다.

이 구성은 환경당 replica 하나다. 기능 측정용이며 연결의 고가용성 검증은 포함하지 않는다.
출발지 앱은 ECR, 목적지는 GHCR을 사용할 수 있지만 같은 앱 digest인지 확인한다.
비공개 GHCR을 익명으로 가져오지 못하면 검증된 동일 digest의 ECR 이미지를 사용한다.

배포와 0% 사전 점검은 [실환경 연결 기록](../../../docs/evidence/t17-edge-connectivity-2026-11-06/README.md)에 있다.
Tunnel 연결만으로 가중치 반영 시간, 실제 분배, 자동 복귀 또는 전체 이전 완료를 판단하지 않는다.

참고: [Cloudflare Kubernetes 구성](https://developers.cloudflare.com/tunnel/guides/kubernetes/),
[token-file 실행 옵션](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/).
