# Platform delivery

## T17 Edge 컨트롤러

`publish-edge-control-image.yml`은 기존 공통 발행/검증/보호 PR 경로로 컨트롤러
이미지를 발행한다. 입력에는 Edge·T10·T19와 고정 T36 소스가 포함되며 실제 발행
영수증이 생기기 전에는 digest 값을 만들지 않는다. `edgeControl.enabled`의 기본값은
false다. 배포값 PR이 병합된 뒤 기존 bootstrap에 `edgeControl.runtime`과 별도
`--edge-adoption`을 제공한다. runtime은 configuration, secrets, journalClaim,
이미지에 포함된 검토 ports factory만 받으며 이미지·명령·replica 우회값은 거부한다.

최초 원장은 `infra/charts/edge-journal`로 생성한다. 실제 플랫폼의 StorageClass를
먼저 확인해 명시하며 원장 PVC는 컨트롤러 rollout/Argo prune 대상이 아니다.
PVC UID·Bound/Filesystem/RWO, namespace·ConfigMap·Secret UID를 bootstrap에서
검사한다. 기존 Deployment/SA가 있으면 해당 UID의 명시적 인수가 필요하다.
Argo controller의 추가 쓰기는 정확히 `t17-edge-control` Deployment/SA만 허용한다.
DB·Secret·PVC는 별도 소유이며 이미지 배포로 교체·삭제하지 않는다.

운영 인증은 플랫폼 Secret으로 mount하는 대상 zone의 Cloudflare LB 제어 자격이다.
토큰 값은 Git·Helm values·Terraform state·로그에 넣지 않는다. 소유 발급 경로로
교체한 후 실제 플랫폼 GET을 확인하고 이전 자격을 폐기한다. 컨트롤러는 등록된
LB/pool 신원과 현재 steering을 매 호출 검사한다. LB/Tunnel 등 자원 자체를 IaC로
관리할 때 실행 중 pool_weights는 이 컨트롤러가 단독으로 소유해야 한다. Terraform
재적용·별도 스크립트·브라우저 조작이 같은 필드를 동시에 덮어쓰면 안 된다.

이번 완료 기준은 실제 플랫폼에서 DB와 native provider 연결 → 동일 plan/run의
현재 승인/T19 소비 → 10/50/100/0 반영 시간 및 T18 실제 분배 → 별도 승인 run의
기준 초과 자동0 → 재시작·단일 실행·최종0 readback이다. Pod Ready, Argo Synced,
이미지 발행만으로 완료 체크하지 않는다. 현재 사람 인증이나 실제 T19 factory가
설치되지 않았다면 그 입력을 채우며 고정 신원·합성 PASS로 배포를 활성화하지 않는다.

T43 owns platform service delivery. The first managed workload is the T10
registration API. T31 still owns approved customer-application changes.

The registry is **private GHCR, pulled directly by the platform**. CI publishes
with its short-lived `GITHUB_TOKEN`. Runtime uses the separate, expiring
`read:packages` credential in the existing `platform-ghcr-reader` Kubernetes
Secret. A classic GitHub package token has account package-read scope; it is not
an AWS OIDC credential or a repository-scoped fine-grained token. Do not use the
publisher token, a human kubeconfig, or a registry password in Helm values.

`charts/registration` owns only the existing `t10-registration` Deployment,
Service and ServiceAccount. It preserves their selectors, runtime identity and
TLS listener. The database, PVCs, collector credentials, registration catalog,
grants and T7 network policies remain separately owned inputs. The catalog's
content hash is a Pod-template annotation so an approved configuration change
causes a rollout. Actual tenant identifiers, endpoints and private network
observations remain protected operational inputs.

The init container can read the approved public source catalog and an empty
checkout volume. It cannot read DB, TLS or collector Secrets. The application
receives the checkout read-only. Both containers run as UID/GID 10001, drop all
capabilities, and have no service-account token or writable root filesystem.

`argocd` pins the upstream Helm chart and runs the controller, repository server
and Redis. There is no public API/UI, Dex or local admin account. ApplicationSet
replicas are zero. Operators use their existing personal Kubernetes access with
Argo CD Core or kubectl; the deployment controller does not impersonate them.
The chart does not create ClusterRoles. A namespace Role and an admission policy
limit platform writes to the three named registration resources. Secrets, DB,
PVCs and other applications are not controller write targets.

Feature work branches from develop and returns through the required CI and Merge
Queue. A successful source build proposes a separate deployment-values PR with
the verified digest. Values-only changes do not rebuild the application. A
release promotes the verified artifact through release/main and back to develop;
a hotfix follows main and then develop. An older build must not overwrite a newer
source revision. Rollback is a protected Git revert followed by actual API and
data-preservation checks, not a rebuilt old tag.

Image proposals retain their source-specific branches. Once the replacement PR
is read back with the expected head, values and publication receipt, the workflow
closes older generated registration-image PRs targeting develop. Recognition checks
the same repository, branch/source binding, T43 title/reference and the exact two
image/publication files; unrelated, fork, release and manually extended PRs are
preserved. Git ancestry prevents a late older run from replacing a newer proposal,
even when their build inputs are identical. The shared publication concurrency
group remains serialized. A rerun reuses the source's existing PR and completes
remaining cleanup; failed creation preserves previous PRs. Lost create/close
responses are reconciled by reading that same branch/PR, without retrying writes.
The delivery result records `closed_proposals`; closing does not delete branches.

PR creation and cleanup use the existing Actions `PROJECT_TOKEN`. GitHub attributes
the PR to that token's account; the Git commit author `bravo-board` does not change
the PR author. A bot identity requires an organization-managed bot or GitHub App
credential through the team's existing credential process. No personal credential
is copied, rotated or replaced by this workflow change.

The publication trigger and its source-freshness check cover every package copied
from the current checkout, including inventory, and the build/delivery recipes.
Repository tests compare these paths with the build context package list so a
new package cannot silently miss publication. Observability remains an explicit
historical source pin; changing that pin or its build recipe triggers publication.

Required infrastructure CI also checks each deployment-values change against the
source in the PR or combined merge group. A proposal that became stale while
waiting cannot promote an image that omits those source changes. Ordinary source
PRs can still merge and trigger their next build. For a rollback, revert the
source and its matching verified image together. This check uses the common
develop ancestor, including promotions from earlier unmerged queue entries.
Changes to delivery-only tooling trigger publication and supersede in-flight
builds, but do not invalidate an already verified image with unchanged build
inputs. A validator-only PR can therefore share a queue group with that image.

Before first adoption, compare resource UIDs and the rendered Deployment with
the live server. Keep automated pruning disabled until owned-resource deletion
and recovery are verified. Do not run the old registration renderer against
resources after Argo CD takes ownership. A publish receipt, Argo `Synced`, a
Ready Pod, actual SSO success, and complete T43 acceptance are separate results.

Local chart contract tests: `uv run infra/gitops/test_registration_chart.py`.
Register the official chart source with
`helm repo add bravo-argo https://argoproj.github.io/argo-helm --force-update`,
then build the pinned dependency with `helm dependency build infra/gitops/argocd`.
Never commit the downloaded chart, rendered private values, credentials or a
cluster kubeconfig.

Operator bootstrap is `python3 infra/gitops/bootstrap.py`. It requires the
expected cluster UID, existing registration Deployment UID, full reviewed
checkout revision and host-local kubeconfig path. Without `--apply` it validates
and renders only. Use Helm 4.0.0. First install without `--runtime-values` creates
the Argo components and CRDs, with both AppProject and Application disabled.
Wait for that release to finish before enabling registration. This avoids
requiring an Argo custom resource before its CRD exists on a fresh cluster.
After the publication-values PR merges, supply a
protected JSON file outside the checkout containing only
`registration: {enabled: true, runtime: {configuration, secrets, hostAliases}}`.
Image or source overrides are rejected. Read an existing non-deployed/pending
Helm release before recovery; do not blindly resubmit a timed-out installation.

Runtime configuration has a separate authority: the versioned registration
catalog/grants generator and the existing Secret issuers. Its input hash is
recorded by bootstrap, and its ConfigMap content hash is recorded in the Pod.
The Argo Application is managed by this bootstrap release; editing it directly
is not a durable configuration change. Secret values never enter that release.
Registry rotation replaces only `platform-ghcr-reader` through the protected
operator transport, verifies an uncached pull, and then revokes the previous
credential. Record the credential expiry without recording its value.

## Product Reader 인수

`publish-product-reader-image.yml`은 Reader의 전체 빌드 입력이 develop에 바뀔 때
기존 공통 발행기 `registration_delivery.py --service product-reader`를 사용한다.
등록 API와 동일한 OCI·SBOM·provenance·scan identity·비공개 GHCR readback 검사와
응답 불명 처리, 소스 최신성 검사, 보호 배포 PR 절차를 재사용한다.
`product-reader-image.json`과 `product-reader-publication.json`은 성공한 발행이
만드는 별도 배포 PR에만 추가된다. 가짜 digest나 초기 완료 영수증을 넣지 않는다.

Reader는 `infra/charts/product-reader`와 `platform-product-reader` Application /
AppProject로 관리한다. 기존 `t10-product-reader` Deployment·Service만 인수한다.
공유 `platform-runtime` SA, DB, PVC, catalog, 기존 Secret·ConfigMap·NetworkPolicy는
생성·갱신·삭제하지 않는다. 모든 입력 mount와 catalog PVC는 읽기 전용이고
초기 prune=false, selfHeal=true다. 공통 controller의 admission allowlist에는
정확히 등록 API와 인수한 Reader 두 이름만 허용하며 Reader는 SA 쓰기 권한이 없다.

기존 bootstrap 런타임 값의 `registration`을 보존하고 `productReader`를 추가한다.
Reader runtime에는 기존 `configuration`(ConfigMap 이름·canonical data SHA256),
`secrets`(database/databaseCA/catalogKey/tls/registry 이름), `catalogClaim`만 둔다.
이미지·명령·namespace·SA를 런타임 입력으로 덮어쓸 수 없다. 값과 인수 파일은
저장소 밖에서 보호하며 토큰·Secret 내용을 복제하지 않는다.

`bootstrap.py --reader-adoption <보호된 JSON>`은 기존 bootstrap 인자에 추가한다.
인수 JSON에는 `deployment_uid`, `service_uid`, `service_account_uid`, `config_uid`,
`catalog_uid`, `secret_uids`(참조 Secret 이름→UID)만 기록한다. 실제 UID, selector,
TLS/catalog/DB mount 참조, canonical 설정 hash와 기존 소유를 대조한 뒤 적용한다.
기존 수동 Reader 렌더러는 인수 뒤 같은 자원을 다시 적용하지 않는다.

배포 완료는 소스·발행 run/attempt·scan·digest·배포 PR·Argo revision/Synced/Healthy와
실제 Pod imageID/config hash/Ready, 현재 권한 거부·정상 조회, 원 기록 보존을 함께
확인한다. 이전 입력의 권한이 만료됐으면 정상 조회 검증은 미완료로 남긴다.
이 인수가 새 계획·grant·모델 호출을 허가하지 않는다. rollback도 동일 서비스의
검증된 publication과 관련 소스를 함께 되돌리는 보호 PR로 수행한다.
