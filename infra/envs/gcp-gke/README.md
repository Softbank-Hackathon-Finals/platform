# T50 GKE 목적지

GCP 프로젝트에 Google 관리형 Kubernetes(GKE) 목적지 클러스터 하나를 terraform으로 펼치고 접는다. 자체 운영 RKE2 목적지는 [`gcp-rke2`](../gcp-rke2)다. 클러스터 위 구성은 T45 스택이 맡고, tailnet 합류는 ADR-0035(T48 WIF)를 따른다. 이름은 A10(`bravo-gcp`·`bravo-gke`)과 겹치지 않는다(`name` 검사).

## 경계

| 항목 | 이 env |
|---|---|
| 노드 | private 노드(외부 주소 없음), Shielded(secure boot), 자동 복구·업그레이드. 나가는 길은 Cloud NAT만, 들어오는 방화벽 규칙은 없다 |
| API | IP endpoint는 private만 쓴다. 운영자는 DNS endpoint로 들어오고, Google이 요청마다 IAM으로 권한을 확인한다. IP 허용 목록은 없다(gcp-rke2의 IAP와 같은 역할) |
| Pod 신원 | Workload Identity(`GKE_METADATA`). Pod는 노드 서비스 계정 토큰을 보지 못하고 `kube-env`도 숨겨진다. gcp-rke2처럼 metadata 차단 정책을 따로 둘 필요가 없다 |
| 네트워크 정책 | Dataplane V2가 NetworkPolicy를 강제한다 |
| 이미지 | 노드 서비스 계정은 GKE 최소 역할(`container.defaultNodeServiceAccount`)과 자기 Artifact Registry 저장소 읽기만 가진다 |
| 발급자 | 클러스터 ServiceAccount 토큰의 공개 OIDC 발급자(`issuer` 출력). Tailscale operator 연합 신원(ADR-0035)과 GCP Workload Identity가 같은 발급자를 신뢰한다. 공개 호스팅이 따로 필요 없다 |
| 프로젝트 고정 | `project_number`가 다르면 계획이 실패한다 |

## 펼치기·접기

비공개 입력(`vars.json`, backend `bucket`·`prefix`)은 Git 밖에 둔다. gcp-rke2와 같은 상태 버킷을 쓰되 prefix는 따로 둔다.

```sh
export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)
terraform init -backend-config=<private>/gke-backend.hcl
terraform plan -var-file=<private>/vars.json -out=<private>/gke-plan.tfplan
terraform apply <private>/gke-plan.tfplan
terraform output -json > <private>/gke-node.json

# kubectl은 gke-gcloud-auth-plugin이 PATH에 있어야 한다(google-cloud-sdk/bin)
KUBECONFIG=<private>/gke-kubeconfig gcloud container clusters get-credentials <cluster> --dns-endpoint --location <zone>

terraform destroy -var-file=<private>/vars.json
```

## 실측 (2026-11-07, 팀 GCP 샌드박스 프로젝트, asia-northeast3-a)

| 항목 | 결과 |
|---|---|
| 펼치기 | 10개 자원, 543초(GKE 컨트롤 플레인과 노드 풀이 대부분) |
| 노드 | e2-standard-4 ×2, v1.35.8-gke(STABLE), amd64, 외부 주소 없음 |
| API | DNS endpoint로 접근, cluster UID 확인 |
| 발급자 | 인터넷에서 `/.well-known/openid-configuration` 200, `issuer` 값 일치 |
| Pod의 metadata | 기본 신원이 Workload Identity pool로 보이고 노드 서비스 계정이 아님, `kube-env` 404 |

비용: GKE 컨트롤 플레인, 노드 2대, 디스크, NAT가 켜져 있는 동안 과금된다. 같은 프로젝트에 A10 `bravo-gke`(vCPU 8)가 있어 할당량 32 중 16을 쓴다. 쓰지 않을 때는 접는다.
