# cluster-gke (A10)

[`cluster/gcp`](../../modules/cluster/gcp)로 GKE Standard를 만들고, `both.identity`의 구축 쪽으로 ESO 컨트롤러(`external-secrets/external-secrets` 서비스 계정)에 Secret Manager 읽기 권한을 Workload Identity로 준다. GSA 키는 없고, 조건으로 ID가 `secret_prefix`(기본 `bravo-`)로 시작하는 비밀만 읽는다.

- 입력 `network` · `subnetwork` · `pod_range_name` · `service_range_name`은 [`infra/stacks/cluster-gke`](../../stacks/cluster-gke/stack.json)가 `gcp-foundation` 출력에서 연결한다. 프로젝트 ID는 개인 tfvars에만 둔다.
- 출력 `cluster`: `name`, `location`, `endpoint`(DNS 엔드포인트), `workload_pool`. 스택은 이 `endpoint`와 kubeconfig의 서버를 대조한 뒤에만 클러스터 단계를 실행한다.
- 상태 backend는 gcp-foundation과 같다(로컬 기본, GCS 선택).
- 생성 순서는 [gcp-foundation README](../gcp-foundation/README.md#실제-생성-순서).
