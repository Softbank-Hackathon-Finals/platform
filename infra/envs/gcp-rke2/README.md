# T50 GCP RKE2 목적지

GCP 프로젝트에 RKE2 노드 하나를 terraform으로 펼치고 접는다. 노드 설치는 T2 온프렘·T7 AWS와 같은 [공통 bootstrap](../../shared/rke2/bootstrap.sh)(CIS 프로필, 고정 릴리스)과 [감사 정책](../../../services/control-runner/templates/rke2-audit-policy.yaml)을 쓴다. 클러스터 위 구성은 T45 `target-onprem` 스택이 맡는다. 이 env는 노드·네트워크·이미지 저장소만 만든다. 종류는 자체 운영 RKE2(onprem 종류)다. GKE 목적지는 A10(#669)이 맡는다. 이름은 `bravo-gcp`·`bravo-gke`와 겹치지 않는다(`name` 검사).

| 파일 | 하는 일 |
|---|---|
| `main.tf` | VPC·서브넷, Cloud NAT(나가는 길만), IAP 범위만 허용하는 방화벽(22·6443), 노드 서비스 계정, Artifact Registry 저장소와 읽기 권한, 외부 주소 없는 VM |
| `cloud-init.sh.tftpl` | 공통 bootstrap, 감사 정책, Artifact Registry용 kubelet 자격 플러그인, Tailscale 설치(로그인 안 함) |
| `node.sh` | IAP·OS Login으로만 노드에 닿는다. `wait`·`ssh`·`params`·`kubeconfig`·`tunnel`·`import-image`. 동사는 [`local-vm.sh`](../onprem/local-vm.sh)와 같다 |
| `tests/boundaries.tftest.hcl` | 외부 주소 없음, IAP 범위만, OS Login, 저장소 읽기만, 다른 프로젝트·A10 이름 거부 |

## 경계

- **들어오는 길:** 공개 주소가 없다. 방화벽은 Google IAP 범위(`35.235.240.0/20`)에서 22·6443만 허용한다. 접근 권한은 IAM(IAP 터널, OS Login)이 정한다. AWS의 SSM 세션과 같은 역할이다. 프로젝트 공용 SSH 키는 막는다. OS Login 키는 8시간 뒤 만료된다.
- **나가는 길:** Cloud NAT만 있다(RKE2 릴리스, 공개 이미지).
- **이미지:** kubelet 자격 플러그인이 metadata 서버에서 노드 서비스 계정 토큰을 받아 이 저장소에서만 pull한다. pull secret, 저장된 비밀번호, 내려받는 바이너리가 없다.
- **노드 토큰 노출:** GCE metadata에는 AWS IMDSv2 hop 1 같은 Pod 차단이 없다. 그래서 Pod도 노드 토큰을 읽을 수 있다. 피해 범위는 IAM으로 줄인다. 노드 서비스 계정에는 이 저장소 읽기 권한 하나만 준다. 실측(아래)에서 Pod가 노드 토큰으로 저장소 읽기 외에는 모두 403을 받았다. 남은 일: 워크로드 Pod의 metadata(`169.254.169.254`) 출구 차단. 이 env는 하지 않는다. Pod를 만들 때 출구를 선언하는 운영화 전략으로 막는다.
- **프로젝트 고정:** `project_number`가 provider 프로젝트와 다르면 계획이 실패한다(AWS `allowed_account_ids`와 같은 역할).

## 펼치기·접기

비공개 입력(`vars.json`: `project_id`, `project_number`)과 backend 설정(`bucket`, `prefix`)은 Git 밖에 둔다. 상태 버킷은 버전 관리와 공개 접근 차단을 켜고 한 번 만든다. 사람마다 `prefix`와 `name`을 다르게 주면 각자 자기 클러스터를 펼치고 접는다.

```sh
export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)   # 확인한 gcloud 계정으로만 실행
terraform init -backend-config=<private>/backend.hcl
terraform plan -var-file=<private>/vars.json -out=<private>/plan.tfplan
terraform apply <private>/plan.tfplan
terraform output -json > <private>/node.json

export BRAVO_GCP_NODE=<private>/node.json
./node.sh wait                                   # cloud-init 끝 + 노드 Ready
./node.sh ssh sudo bash -s < ../onprem/verify-node.sh
./node.sh params                                 # T45 스택 params + cluster UID
./node.sh tunnel 57443 &                         # API
./node.sh kubeconfig <private>/kubeconfig 57443

terraform destroy -var-file=<private>/vars.json   # 접기. 이 상태의 자원만 지운다
```

ADC(`application_default_credentials.json`)의 quota 프로젝트가 다른 프로젝트를 가리키면 GCS backend가 404를 낸다. 그래서 위처럼 gcloud 계정의 단기 토큰을 쓴다. 이미지는 노드 아키텍처(amd64)에 맞춰 빌드한다. `import-image`도 같다.

## 실측 (2026-11-07, 팀 GCP 샌드박스 프로젝트, asia-northeast3-a, e2-standard-8)

| 항목 | 결과 |
|---|---|
| 첫 펼치기 | apply 63초, RKE2 Ready까지 144초 더 |
| 접기 | destroy 9개 자원 130초. 같은 프로젝트의 A10 GKE는 그대로 |
| 다시 펼치기 | apply 85초, Ready까지 전체 227초. 새 cluster UID |
| `verify-node.sh` | 12/12 PASS(두 번 모두) |
| 이미지 | Artifact Registry digest pull 4.4초, 클러스터 pull secret 0개, restricted PSA |
| Pod에서 노드 토큰 | storage·compute·IAM·GKE·Secret Manager 목록 403, 저장소 push 403, 자기 저장소 읽기 200 |

같은 demo-chat 커밋을 다시 빌드하면 digest가 달라졌다. 후보 이미지는 한 번 빌드한 digest를 끝까지 쓴다.

비용: 노드(e2-standard-8)·디스크·NAT가 켜져 있는 동안 과금된다. 프로젝트 예산 알림은 알림일 뿐 상한이 아니다. 쓰지 않을 때는 접는다.
