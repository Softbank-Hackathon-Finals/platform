# Platform delivery

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

Before first adoption, compare resource UIDs and the rendered Deployment with
the live server. Keep automated pruning disabled until owned-resource deletion
and recovery are verified. Do not run the old registration renderer against
resources after Argo CD takes ownership. A publish receipt, Argo `Synced`, a
Ready Pod, actual SSO success, and complete T43 acceptance are separate results.

Local chart contract tests: `uv run infra/gitops/test_registration_chart.py`.
Build the pinned Argo dependency first with `helm dependency build infra/gitops/argocd`.
Never commit the downloaded chart, rendered private values, credentials or a
cluster kubeconfig.

Operator bootstrap is `python3 infra/gitops/bootstrap.py`. It requires the
expected cluster UID, existing registration Deployment UID, full reviewed
checkout revision and host-local kubeconfig path. Without `--apply` it validates
and renders only. Use Helm 4.0.0. First install without `--runtime-values` leaves
the Application disabled. After the publication-values PR merges, supply a
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
