# gke-secret-reader (A10)

[`cluster-gke` 스택](../../stacks/cluster-gke/stack.json)의 두 번째 terraform 단계다. 클러스터 · 네트워크 · 노드 신원은 T50 [`gcp-gke`](../gcp-gke/README.md)가 만들고, 이 env는 `both.identity`의 구축 쪽만 더한다: ESO 컨트롤러(`external-secrets/external-secrets` 서비스 계정)가 Workload Identity로 Secret Manager를 읽는 권한. GSA 키는 없고, 조건으로 ID가 `secret_prefix`(기본 `bravo-`)로 시작하는 비밀만 읽는다. `secretmanager.googleapis.com`을 켜되 접을 때 끄지 않는다.

- `project_id`는 스택이 `gcp-gke`의 `project_id` 출력에서 연결한다. `project_number`는 gcp-gke와 같은 개인 tfvars 값이고, 다른 프로젝트면 계획이 실패한다.
- Workload Identity pool은 프로젝트 하나(`<project>.svc.id.goog`)라 이 권한은 같은 프로젝트의 모든 GKE 클러스터의 같은 서비스 계정에 닿는다. 스택은 쪽(side)마다 클러스터 이름을 `bravo-<side>-gke`로 나눈다.
- backend는 gcp-gke와 같은 부분 GCS(개인 `secret-reader.backend.hcl`의 `bucket` · `prefix`)다.
- 스택의 개인 입력 폴더(저장소 밖, 0700): `cluster.backend.hcl` · `cluster.tfvars.json`(gcp-gke: `project_id`, `project_number`, 필요 시 `zone` · `machine_type` · `node_count`. `name`은 스택이 정한다), `secret-reader.backend.hcl` · `secret-reader.tfvars.json`(`project_number`). 쪽마다 backend `prefix`를 나눈다. 필요한 API(`compute` · `container` · `artifactregistry`)와 권한은 [gcp-gke README](../gcp-gke/README.md)를 따른다.
