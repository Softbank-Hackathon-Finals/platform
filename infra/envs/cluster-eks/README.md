# cluster-eks (A10)

[`cluster/aws`](../../modules/cluster/aws)로 EKS를 만들고, `both.identity`의 구축 쪽으로 ESO 컨트롤러에 Secrets Manager 읽기 권한을 EKS Pod Identity로 준다(이름이 `secret_prefix`, 기본 `bravo/`로 시작하는 비밀만). 역할 ARN이 매니페스트에 들어가지 않는다. 앱 워크로드용 IRSA OIDC 공급자도 만든다(출력 `oidc_provider_arn`).

- 입력 VPC · 사설 서브넷 2개 · 라우팅 테이블은 [`infra/stacks/cluster-eks`](../../stacks/cluster-eks/stack.json)가 `aws-foundation` 출력(`source` VPC)에서 연결한다. 계정마다 개인 backend 파일로 상태를 나눈다.
- 이미 `source-eks`가 NAT 기본 경로를 넣은 VPC에 두 번째 클러스터를 만들 때는 `create_nat = false`(같은 라우팅 테이블에 기본 경로를 두 번 넣지 않는다)와 다른 `name`을 개인 tfvars에 준다.
- backend는 다른 AWS 스택과 같이 S3(개인 `cluster.backend.hcl`)다. 계정 ID · 운영자 principal은 개인 tfvars에만 둔다.
- 생성 순서: 개인 입력 준비 → `cluster-eks` 스택 `plan` · 승인 · `run`(첫 단계 `aws-foundation`, 이미 있으면 `SKIPPED`) → `cluster-access`에서 `aws eks update-kubeconfig --name <name> --region <region>` 뒤 `confirm`. 실제 생성은 A11.
