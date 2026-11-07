# A11 · GKE 공통 기반 실환경 설치와 확인 (2026-11-07)

A10(#669)의 [`infra/stacks/cluster-gke`](../../../infra/stacks/cluster-gke/stack.json)가 정의한 공통 기반을 실제 GKE 클러스터에 올리고 확인했다. 이 기록은 그 위에 쌓은 `feature/A11-gke-base` 브랜치의 증빙이다.

- 대상: 사용자 승인으로 A10 Terraform(`gcp-foundation` → `cluster-gke`)이 만든 GKE Standard 클러스터 1개(`asia-northeast3-a`, `e2-standard-4` × 2, Kubernetes `v1.36.4-gke`). 이하 `<CLUSTER>`, 프로젝트는 `<PROJECT_ID>`, 실행 계정은 `<ACCOUNT>`로 가린다.
- 역할(`side`): `target`(목적지). 앱 namespace `target-dev`, DB `target-db`.
- 이 GKE와 이 GCP 프로젝트만 다뤘다. 다른 kubeconfig context, 팀 AWS 계정, EKS, 온프렘 클러스터는 건드리지 않았다. 프로젝트에 A11과 무관한 디스크가 하나 있어 그대로 두었다.

## 어떻게 설치했나 (T45 실행기 전체를 쓰지 않은 이유)

T45 실행기(`bravo-infra-plan run`)는 단계마다 T10 `approval_kind=infra` 승인, 목적지 Argo CD(T44), 비공개 설정 저장소 deploy key를 요구한다. 이번 확인 범위(공통 기반이 실제 GKE에서 뜨는가)에는 이 셋이 없어서 실행기 전체를 거치지 않았다. 대신 **실행기의 부품을 그대로** 썼다([`apply_steps.py`](apply_steps.py)).

| 실행기 단계 | 이번에 쓴 것 | 같은 점 / 다른 점 |
|---|---|---|
| `gitops_publish` 렌더 | `Planner._render` | 같은 렌더러, 같은 chart 묶음과 sha256 검사, `helm template --kube-version v1.36.4 --include-crds` |
| `gitops_sync`(Argo CD) | `KubectlCluster.apply` | 실행기의 직접 적용 경로와 같은 server-side apply(field manager `bravo-infra-plan`). Argo CD Application은 없다 |
| `secret_generate` | 실행기의 `_random` + `KubectlCluster.create_secret` | 같다(만들기만 하고 덮어쓰지 않음, 값 출력 없음) |
| `wait` · `probe` | `kubectl wait`, `kubectl get` | 조건은 스택과 같다(`Cluster Ready`, `SecretStore Ready`) |

chart는 스택의 `source`에서 받아 스택에 고정된 sha256과 대조했다.

| chart | sha256 (스택 고정값과 일치) |
|---|---|
| `cloudnative-pg-0.29.1.tgz` | `b53d3991fe84bcf38767e7702cae78666265427a127a26fff168ab4207d2b1df` |
| `external-secrets-2.14.0.tgz` | `0a6d9e91b1ff50adfdbf24b50b582babfa7c5b7df42b21974ca4b3fb88ee5e34` |

렌더 결과(`manifest_text` sha256, 수정 반영 후 브랜치 기준):

| 파일 | 종류 | sha256 |
|---|---|---|
| `base/namespaces.json` | Namespace 2, ServiceAccount 1 | `fb9c619167c499e1b4f31d7e3a603b8545c0f11e2932a3fad0a14beed8e47fb9` |
| `base/tailscale-namespace.json` | Namespace 1 | `ce2dd62ed8fda786cc9a76c764b46018ce820d15420b2ecac248e6b747fb4718` |
| `cnpg/operator.json` | CRD 11, Deployment 1, webhook 2 등 23개 | `526053231f85b2823e85468ffd951e3e6fa1c1d66068dc44341cd8b8d0968468` |
| `database/database.json` | StorageClass 1, ConfigMap 1, Cluster 1 | `4dbfed3abce92087d072da49b405ade1d32704589422763d8c6b7e05fe59c2d6` |
| `eso/controller.json` | CRD 25, Deployment 3 등 44개 | `101b22880a7d6892d6776a824bf6a3e67d755ee83eafd13e72982e44fbd4ed16` |
| `secret-store/store.json` | SecretStore 1 | `85bb2835d14758b5bf67c752409dbe325c2cc43a9eb0df91699eb2ae69803ee0` |

## 실행한 명령 (값은 가림)

```sh
# 접속(모든 명령 공통). 전역 gcloud 설정은 바꾸지 않았다
export KUBECONFIG=<비공개 폴더>/gke.kubeconfig CLOUDSDK_CORE_ACCOUNT=<ACCOUNT>

# 0. cluster-access: kubeconfig 서버 = terraform 출력 endpoint(DNS 엔드포인트) → 일치
# 1. chart 받기와 대조
helm pull cloudnative-pg --repo https://cloudnative-pg.github.io/charts --version 0.29.1
helm pull external-secrets --repo https://charts.external-secrets.io --version 2.14.0
shasum -a 256 *.tgz

# 2. 스택 순서대로 적용(공통 인자: --repo . --charts <chart 폴더> --private-dir <비공개 폴더>
#    --project-id <PROJECT_ID> --kube-version v1.36.4, side=target)
uv run --project services/infra-plan python docs/evidence/a11-gke-base-2026-11-07/apply_steps.py apply base ...
uv run --project services/infra-plan python docs/evidence/a11-gke-base-2026-11-07/apply_steps.py secrets db-secrets ...
uv run --project services/infra-plan python docs/evidence/a11-gke-base-2026-11-07/apply_steps.py apply cnpg ...
kubectl -n cnpg-system rollout status deploy/cnpg-cloudnative-pg
uv run --project services/infra-plan python docs/evidence/a11-gke-base-2026-11-07/apply_steps.py apply database ...
kubectl -n target-dev wait cluster.postgresql.cnpg.io/target-db --for=condition=Ready --timeout=600s
uv run --project services/infra-plan python docs/evidence/a11-gke-base-2026-11-07/apply_steps.py apply eso ...
kubectl -n external-secrets rollout status deploy/external-secrets deploy/external-secrets-webhook deploy/external-secrets-cert-controller
uv run --project services/infra-plan python docs/evidence/a11-gke-base-2026-11-07/apply_steps.py apply secret-store ...

# 3. ESO 확인용 비밀(값은 무작위, 출력하지 않음. 라벨 component=a11)
openssl rand -hex 24 | gcloud secrets create bravo-a11-probe      --project <PROJECT_ID> --replication-policy=automatic --labels=component=a11 --data-file=-
openssl rand -hex 24 | gcloud secrets create a11-probe-unprefixed --project <PROJECT_ID> --replication-policy=automatic --labels=component=a11 --data-file=-
kubectl apply -f <ExternalSecret 2개: a11-probe → bravo-a11-probe, a11-probe-unprefixed → a11-probe-unprefixed>
# 동기화 값 대조: 두 값의 sha256 앞 12자리만 비교했다(값 자체는 출력하지 않음)
kubectl -n target-dev get secret a11-probe -o jsonpath='{.data.value}' | base64 -d | shasum -a 256
gcloud secrets versions access latest --secret bravo-a11-probe --project <PROJECT_ID> | shasum -a 256
```

## 결과

### 노드 · Pod

| 대상 | 상태 |
|---|---|
| 노드 2개 | `Ready`, `v1.36.4-gke.2046003`, Container-Optimized OS, containerd 2.2.7 |
| `cnpg-system/cnpg-cloudnative-pg` | 1/1 Running, 이미지 `cloudnative-pg:1.30.1@sha256:923c267e…`(팀 온프렘과 같은 digest) |
| `external-secrets/*` (controller, webhook, cert-controller) | 각 1/1 Running, 이미지 `external-secrets:v2.14.0@sha256:10608eb5…`. `external-secrets` namespace PSS `restricted`에서 그대로 뜸 |
| `target-dev/target-db-1` | 1/1 Running(PSS `restricted`) |

### CNPG

- `Cluster target-dev/target-db`: instances 1, ready 1, `Cluster in healthy state`, 조건 `Ready` 충족(적용 후 약 1분). 이미지 `postgresql:18.4-system-trixie@sha256:42708a75…`.
- managed roles: `chat_app` · `chat_replicator` **reconciled**(비밀 `target-app-db` · `target-replication`은 실행기 생성기로 만들었다).
- 볼륨: PVC `target-db-1` 8Gi `Bound`(처음 적용한 `standard-rwo`). 아래 결함 1 참고.
- 사설 노드 GKE에서도 CNPG webhook(포트 9443)이 API 서버에서 호출됐다(별도 방화벽 규칙 없이 `Cluster` 생성 성공).

### ESO + Secret Manager (Workload Identity)

| 확인 | 결과 |
|---|---|
| `SecretStore target-dev/bravo-cloud-secrets` | `Valid`, `Ready=True`(인증 블록 없음, 컨트롤러 KSA의 Workload Identity) |
| `ExternalSecret a11-probe` → `bravo-a11-probe` | `SecretSynced`, `Ready=True`. Kubernetes Secret 값과 Secret Manager 값의 sha256 일치 |
| `ExternalSecret a11-probe-unprefixed` → `a11-probe-unprefixed` | `SecretSyncedError`, Secret 생성 안 됨. 컨트롤러 로그: `PermissionDenied ... 'secretmanager.versions.access' denied` |
| 프로젝트 IAM의 Secret Manager 바인딩 | `roles/secretmanager.secretAccessor` 1건, 조건 `bravo-secret-prefix`, 주체 `…/subject/ns/external-secrets/sa/external-secrets` 뿐 |

`bravo-` 접두사 조건이 실제로 막는다는 것을 거부 시험으로 확인했다. 프로젝트에서 Secret Manager 읽기 권한을 가진 주체는 ESO 컨트롤러의 Workload Identity 하나뿐이다.

## 발견한 결함과 수정

1. **GKE DB 디스크에 `component` 라벨이 없었다(수정).** 렌더러가 CNPG `Cluster`에 GKE 기본 `standard-rwo`를 썼고, 이 클래스로 PD CSI가 만든 디스크에는 GKE 내부 라벨만 붙고 `component`가 없었다(AGENTS.md: 모든 클라우드 자원에 `component`).
   - 수정: [`render.py`](../../../infra/envs/cluster-base/render.py)가 GKE에도 자체 StorageClass `bravo-pd-balanced`(`pd.csi.storage.gke.io`, `type=pd-balanced`, `labels=component=<side>`, Retain, WaitForFirstConsumer)를 내고 `Cluster`가 그것을 쓴다. EKS `bravo-gp3`에도 같은 이유로 `tagSpecification_1=component=<side>`를 더했다(EKS 실측은 아직 안 함). 테스트(`test_cluster_stacks.py`)가 클래스 이름과 디스크 라벨 파라미터를 확인한다.
   - 실측: `bravo-pd-balanced`를 적용하고 확인용 PVC `a11-sc-probe`(10Gi) + Job으로 디스크를 만들게 했더니 GCP 디스크 라벨 `component=target`이 붙었다. 수정한 `database` 렌더는 server-side dry-run으로 CNPG webhook이 받아들임을 확인했다.
   - 이미 만들어진 DB 디스크는 `gcloud compute disks add-labels … --labels=component=a11`로 라벨만 달았다. 살아 있는 `Cluster`의 `storageClass`는 바꾸지 않았다(기존 PVC는 클래스를 바꿀 수 없고, 다음 실측에 쓸 DB를 다시 만들지 않기 위해서). 다음 실행기 적용 때 `Cluster` spec만 `bravo-pd-balanced`로 바뀌고 기존 PVC는 그대로 남는다.

결함은 아니지만 적어 둘 점:

- 스택은 비공개 폴더에서 `cluster.backend.hcl` · `cluster.tfvars.json`을 찾는데, 이번 Terraform은 `cluster-gke.*` 이름으로 실행됐다. 실행기로 이어갈 때는 파일 이름을 스택에 맞춰야 한다.
- `cnpg-system` namespace는 planner가 만들어 `component` 라벨이 없다(쿠버네티스 객체, 기존 테스트도 예외로 둔 부분).
- `kube_version` 파라미터는 `v1.36.4`처럼 GKE 접미사(`-gke.…`)를 떼고 넣어야 패턴을 통과한다.

## 클러스터에 남긴 것 (다음 실측에 쓴다, 지우지 않음)

- namespace `target-dev`(restricted), `external-secrets`(restricted), `tailscale-system`(privileged, 비어 있음), `cnpg-system`
- CNPG operator, `Cluster target-db`와 비밀 2개, ESO 3개 Deployment, `SecretStore bravo-cloud-secrets`, StorageClass `bravo-pd-balanced`
- 확인용: `ExternalSecret a11-probe` · `a11-probe-unprefixed`, PVC · Job `a11-sc-probe`(완료) — 모두 `component=a11`
- GCP: Secret Manager 비밀 `bravo-a11-probe` · `a11-probe-unprefixed`(라벨 `component=a11`), 동적 PD 2개(`component=a11` / `component=target`)

## 남은 일

- **PgBouncer**: 붙이지 않았다. 온프렘 방식([`target-workloads/pooler.py`](../../../infra/envs/target-workloads/pooler.py))은 컷오버 전 풀러라 Tailscale egress Service(`source-for-pooler`)의 주소 · UID, 출발지 풀러에서 옮겨 올 인증 Secret, 컷오버 실행기의 `cutover-backend-ca`가 있어야 렌더된다. Tailscale operator(사람의 OAuth 단계)가 먼저다. 또 스택(`cluster-gke`)에 PgBouncer 단계가 아직 없다(ADR-0034 3.2 규칙 3은 PgBouncer를 공통 기반에 포함).
- **Tailscale operator · cloudflared**: 사람 단계(OAuth, Tunnel 토큰)라 이번에 설치하지 않았다. 따라서 스택의 `verify`(cloudflared Available)는 아직 확인하지 않았다.
- **T45 실행기 전체 경로**: T10 승인 · 목적지 Argo CD · 설정 저장소 자격을 갖춘 뒤 같은 스택을 `bravo-infra-plan run`으로 돌려 `gitops_sync` 단계를 확인한다. 이번에 server-side apply한 객체는 같은 field manager라 실행기 diff에서 `Cluster`의 `storageClass` 변경 외에는 차이가 없어야 한다.
- **EKS 디스크 태그**: `tagSpecification_1`은 EKS 실환경에서 아직 확인하지 않았다.
