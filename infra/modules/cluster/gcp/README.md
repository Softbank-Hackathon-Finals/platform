# GCP cluster module

`cluster/gcp`(A10, ADR-0034): 출발지 · 목적지 어느 쪽에도 쓰는 GKE Standard.

- **Standard를 고른 이유**: Tailscale operator 프록시가 `NET_ADMIN`을 요구하는데 Autopilot은 이를 막는다. CNPG는 Standard 노드의 기본 `standard-rwo`(pd-balanced) 디스크로 그대로 뜬다.
- 노드 풀 하나(`e2-standard-4` × 2 기본, 영역 클러스터 `asia-northeast3-a`). 사설 노드 + Cloud NAT(`network/gcp`), Dataplane V2(NetworkPolicy 강제), Shielded 노드.
- **Workload Identity**: 클러스터 `workload_pool = <project>.svc.id.goog`, 노드 `GKE_METADATA`. 워크로드 권한은 GSA 없이 Kubernetes 서비스 계정 principal에 IAM을 직접 준다(`envs/cluster-gke`의 ESO 읽기 권한).
- **제어부 접근**: IP 엔드포인트를 끄고 DNS 엔드포인트(IAM 인증)만 연다. `gcloud container clusters get-credentials <name> --location <location> --dns-endpoint`.
- **라벨**: 클러스터 · 노드 · 디스크에 `labels`(필수 `component`). 노드 전용 GSA는 로그 · 지표 · 이미지 읽기 권한만 갖는다.
- `deletion_protection = false`: 데모 뒤 `terraform destroy`를 위해서다.
- 출력 `cluster.endpoint`(DNS 엔드포인트)는 스택이 kubeconfig의 서버와 대조해 다른 클러스터에 적용하지 않게 한다.
