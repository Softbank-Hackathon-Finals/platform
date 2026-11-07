# GCP foundation (A10)

GCP 프로젝트 하나에 클러스터 스택이 쓰는 기반을 만든다: 필수 API(`compute` · `container` · `secretmanager` · `iam` 등)와 [`network/gcp`](../../modules/network/gcp)(VPC, 서브넷 + GKE 보조 범위, Cloud NAT). 리전 기본값은 `asia-northeast3`. 모든 라벨 가능한 자원에 provider `default_labels`로 `component`(기본 `cluster-gke`)를 단다.

- 프로젝트 ID는 저장소 밖 tfvars에만 둔다. 이 폴더와 스택에는 값이 없다.
- 상태는 **로컬 기본**이다. backend 파일(`<private_dir>/gcp-foundation.backend.hcl`)에 `path = "/저장소 밖/gcp-foundation.tfstate"`를 적는다. 공유 상태가 필요하면 이 폴더에 추적하지 않는 `backend_override.tf`(`terraform { backend "gcs" {} }`)를 두고 backend 파일에 `bucket` · `prefix`를 적는다(`.gitignore`가 `backend_override.tf`를 막는다). 이때 코드 digest가 바뀌므로 계획을 다시 만든다.
- 이 폴더는 단독 실행도, [`infra/stacks/cluster-gke`](../../stacks/cluster-gke/stack.json)의 첫 terraform 단계로도 쓴다. 출력 `foundation`이 다음 단계(`infra/envs/cluster-gke`)의 `network` · `subnetwork` · `pod_range_name` · `service_range_name`으로 연결된다.

## 실제 생성 순서

실제 생성은 사용자 확인 뒤 A11에서 한다. 이 PR(A10)은 `fmt` · `validate` · 오프라인 계획까지만 했다.

1. **프로젝트와 결제 연결(사람, 콘솔 또는 gcloud)**
   ```sh
   gcloud auth login                         # 개인 계정, 장기 키 없음
   gcloud projects create <PROJECT_ID> --labels=component=cluster-gke
   gcloud billing projects link <PROJECT_ID> --billing-account=<BILLING_ACCOUNT_ID>
   gcloud config set project <PROJECT_ID>
   gcloud auth application-default login     # terraform이 쓰는 ADC
   ```
   필요한 권한: 프로젝트 `roles/owner`(또는 `roles/editor` + `roles/resourcemanager.projectIamAdmin` + `roles/serviceusage.serviceUsageAdmin` + `roles/container.admin`), 결제 계정 `roles/billing.user`. 예산 경보(`gcloud billing budgets create`)도 이때 건다.
2. **개인 입력 폴더**(저장소 밖, 0700): `gcp-foundation.backend.hcl`(`path = ...`), `gcp-foundation.tfvars.json`(`{"project_id": "<PROJECT_ID>"}`), `cluster.backend.hcl`, `cluster.tfvars.json`(`{"project_id": "<PROJECT_ID>"}` + 필요 시 `name` · `location`).
3. **gcp-foundation apply** — 둘 중 하나:
   - 단독: `terraform -chdir=infra/envs/gcp-foundation init -backend-config=<private>/gcp-foundation.backend.hcl` → `plan -var-file=<private>/gcp-foundation.tfvars.json -out=...` → 사람이 계획을 보고 `apply <plan>`.
   - 스택: 아래 4의 첫 단계(`gcp-foundation`)가 같은 일을 한다. 이미 단독으로 적용했으면 변경 없음(`SKIPPED`).
   **데모 최소 크기(기본값)**: 영역 GKE Standard 클러스터 1개(`asia-northeast3-a`) + 노드 풀 1개 `e2-standard-4` × 2(vCPU 8, 리전 CPU 할당량 100 안), pd-balanced 50GB. 바꿀 때만 `cluster.tfvars.json`에 `machine_type` · `node_count`(1~3)를 적는다.
4. **cluster-gke 스택 실행**(T10 `approval_kind=infra` 승인 필요)
   ```sh
   cd services/infra-plan
   uv run bravo-infra-plan plan ../../infra/stacks/cluster-gke/stack.json --request <req.json> \
     --repo-root ../.. --charts-dir <chart 폴더> --stack-revision <SHA> --base-commit <설정 저장소 SHA> \
     --target project_id=<PROJECT_ID> --out <새 폴더>
   uv run bravo-infra-plan run ../../infra/stacks/cluster-gke/stack.json --request <req.json> \
     --migration-plan-id <plan> --approval-id <id> --run-id <run> --started-by <me> \
     --repo-root ../.. --runs-dir <dir> --t10-url <url> --charts-dir <chart 폴더> \
     --kubeconfig <빈 kubeconfig 파일> --config-repo <설정 저장소> --config-workdir <dir>
   ```
   빈 프로젝트의 첫 실행은 앞 단계 출력이 없어 `cluster`가 `PENDING_UPSTREAM` → `HOLD`(`terraform_change_set_unapproved`)한다. 새 계획 · 승인으로 이어간다(레벨마다 승인, T45 규칙). `cluster` 적용 뒤 `cluster-access`에서 사람이 `gcloud container clusters get-credentials <name> --location <location> --dns-endpoint`로 kubeconfig를 받고 `confirm`한다. 이후 Argo CD(T44) · 설정 저장소 자격 · Tailscale OAuth · Tunnel 토큰은 사람 단계이고 나머지는 GitOps로 진행된다.
5. **확인**: `verify`(DB Ready, cloudflared Available), `secret-store-ready`(ESO가 Workload Identity로 Secret Manager에 인증).
6. **정리**: 데모 뒤 `terraform destroy`는 사람이 한다(cluster → gcp-foundation 순서). API는 끄지 않는다(`disable_on_destroy = false`).
