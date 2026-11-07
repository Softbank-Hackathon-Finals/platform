# cluster-base renderer (A10)

관리형 클러스터(GKE · EKS)의 공통 기반 매니페스트를 `side`(`source` · `target`)별로 렌더한다. 클러스터에 접속하지 않고 비밀 값을 내지 않는다. [`infra/stacks/cluster-gke`](../../stacks/cluster-gke/stack.json) · [`cluster-eks`](../../stacks/cluster-eks/stack.json)가 부른다.

| 단계 | 내용 |
|---|---|
| `namespaces` | 앱 namespace `<side>-dev`(restricted)와 토큰 없는 default 계정, `external-secrets` namespace |
| `database --storage gke\|eks` | `target-workloads/render.py`의 CNPG Cluster에서 로컬 PV · StorageClass를 빼고 클라우드 동적 볼륨을 쓴다. StorageClass(`bravo-pd-balanced` / gp3 `bravo-gp3`, Retain)를 함께 내고, CSI 파라미터로 만들어지는 디스크에 `component=<side>`를 단다(A11: 기본 `standard-rwo`로 만든 PD에 라벨이 없었다) |
| `cnpg-values` · `eso-values` | 기존 values에서 감시 namespace만 `<side>-dev`로 바꾼다 |
| `secret-store --provider gcp\|aws` | 앱 namespace의 `SecretStore bravo-cloud-secrets`. 인증 블록 없이 ESO 컨트롤러의 워크로드 신원(GKE Workload Identity / EKS Pod Identity)으로 접근한다 |

DB 모양과 ESO 버전의 원본은 `target-workloads` · `target-secrets` 렌더러다. 여기서는 이름과 저장소만 바꾼다.
