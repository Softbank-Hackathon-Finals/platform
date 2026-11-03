# ADR-0011: Private image delivery with temporary identities

- Date: 2026-11-03
- Status: Accepted for the AWS development environment
- Task: T7, platform issue #12; T6 consumer integration

## Context

The reviewed demo-chat source builds into a scanned GHCR image with immutable
digest, provenance and SBOM. The organization disables public package visibility.
The user chose to retain that policy and prepare private access. Copying a
personal registry token or a long-lived pull secret into both clusters would
not satisfy the team's temporary-identity boundary.

## Decision

Keep private GHCR as the original publication. Mirror the exact OCI index,
application manifest and attestation manifests into one private AWS ECR
repository without rebuilding or changing any digest.

A dedicated main-only GitHub OIDC publisher role may upload/read this exact
repository. A job-scoped `GITHUB_TOKEN` reads GHCR and a 900-second AWS session
accesses ECR. The workflow validates the reviewed source receipt, rescans the
downloaded bytes, and hashes every referenced blob before and after copying.
Its public evidence excludes private account/registry identifiers and credentials.

The new registry state reuses the existing GitHub identity provider and does
not own foundation EC2/EKS roles, networks or organization policy. EKS keeps
its existing node pull permissions. The RKE2 foundation owner grants read access
to only this repository. A kubelet ECR credential provider obtains credentials
through the instance role and returns them in memory; no shared pull secret is
created. RKE2 configuration is an explicit separate operational change with
an original-config backup, checksummed provider binary and readiness readback.

## Consequences and evidence boundaries

The AWS development path preserves the source digest and adds a private mirror
with its storage/transfer costs. It does not make ECR the required destination
registry for the on-premises exit scenario. That destination still needs its
own private access or registry-transfer design with matching digest verification.

Successful CI transfer/pull, actual EKS application verification, and RKE2
kubelet image pulls are recorded separately. Two new pods can prove current
kubelet/provider integration, but do not prove behavior after the ECR token's
12-hour expiry. The provider's cache metadata and documented refresh logic
must be distinguished from an expiry-and-renewal observation.

Sources: [ECR OCI formats and digest pulls](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-manifest-formats.html),
[temporary registry authentication](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html),
[Kubernetes credential providers](https://kubernetes.io/docs/tasks/administer-cluster/kubelet-credential-provider/).
