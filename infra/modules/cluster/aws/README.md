# AWS cluster module

`cluster/aws`(A10, ADR-0034): 출발지 · 목적지 어느 쪽에도 쓰는 EKS. [`envs/source-eks`](../../../envs/source-eks)를 일반화했고 source-eks는 그대로 둔다(이미 적용된 상태가 있다).

| 구성 | 내용 |
|---|---|
| 클러스터 | API 인증 모드, 생성자 자동 admin 끔, 운영자 개인 principal만 access entry로 admin, KMS 봉투 암호화, 제어부 로그 7일 |
| 엔드포인트 | 사설 기본. `endpoint_public_access_cidrs`를 주면 그 CIDR만 공개 엔드포인트(0.0.0.0/0 거부) |
| 신원 | IRSA OIDC 공급자(`oidc_provider_arn` 출력), EKS Pod Identity agent(역할 ARN을 매니페스트에 넣지 않고 namespace/SA에 역할 부여) |
| 저장소 | EBS CSI add-on(Pod Identity). CNPG 볼륨은 gp3 StorageClass(`infra/envs/cluster-base/render.py`)로 받는다 |
| 망 | 사설 서브넷 2개 입력. `create_nat=true`면 IGW · 공개 egress 서브넷 · NAT를 만든다(source-eks와 같은 방식) |

- provider를 선언하지 않는다. root의 `allowed_account_ids`와 `default_tags`(`component`)를 상속한다. 노드 인스턴스 · 볼륨 태그만 `component` 입력으로 직접 단다.
- 출력 `cluster`는 `aws eks update-kubeconfig`와 스택의 클러스터 결속(`endpoint`)에 쓴다. 자격은 담지 않는다.
- 실행 root는 [`envs/cluster-eks`](../../../envs/cluster-eks), 스택은 [`stacks/cluster-eks`](../../../stacks/cluster-eks).
