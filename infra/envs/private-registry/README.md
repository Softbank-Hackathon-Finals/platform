# Private development image registry

T7 keeps the organization's GHCR visibility policy unchanged. This separate
state creates `bravo/demo-chat` in private ECR and an upload-only GitHub OIDC
publisher role. It reuses the foundation's GitHub provider as a data source.
It does not own EKS/EC2 roles, the provider, network resources, organization
settings, or any long-lived credential.

The repository uses immutable tags, KMS encryption with ECR's AWS-managed key,
on-push scanning and deletion protection. There is no automatic image expiry:
the index and its attestation manifests are retained as deployment evidence.
The role can authenticate to ECR and upload/read only this repository; it
cannot create repositories, change repository policies or delete images.
The trust requires the STS audience and exact immutable GitHub organization,
repository identities and the main branch. There is no branch wildcard or
bootstrap feature-branch subject in the final configuration.

## State and inputs

Use a new S3 state key, `runtime/private-registry/terraform.tfstate`, in the
existing operator-managed backend with encryption and locking. Never reuse a
foundation key. Supply `account_id`, `github_owner_id`, `github_repository_id`
through an operator-private variable file.
The provider rejects an AWS account mismatch. Confirm the existing platform
VPC/instance owner and GitHub repository OIDC subject settings before applying.

```sh
terraform init -backend-config="$PRIVATE_BACKEND_FILE"
terraform plan -var-file="$PRIVATE_INPUT_FILE" -out="$PRIVATE_PLAN_FILE"
terraform apply "$PRIVATE_PLAN_FILE"
terraform output -json registry_contract > "$PRIVATE_CONTRACT_FILE"
```

Keep these files, raw Terraform plans and the output contract out of Git.
The output contains account/resource identifiers and an exact-repository pull
policy for the foundation owner. Never print it into public CI or a PR.

## Delivery and consumers

The demo-chat transfer workflow reads a previously scanned private GHCR image
with its temporary `GITHUB_TOKEN`, obtains a short-lived AWS role through
OIDC, and copies the entire OCI index using `skopeo --all --preserve-digests`.
No rebuild or manifest conversion is permitted. The destination digest and
both child manifests must match the reviewed source. Account IDs and private
registry URLs belong in operator-private configuration, not public receipts.

EKS already has a foundation-owned ECR pull policy. Its node credential
provider obtains registry authentication; application IRSA is not the image
pull identity. RKE2's foundation owner adds exact-repository pull permissions;
the operator installs the kubelet credential provider described below. A single authenticated
cache pull proves only that attempt, not credential refresh or future pod
scheduling. Do not store a 12-hour ECR password as a long-lived shared pull
secret. Non-AWS destination clusters need their own bounded access design.

## RKE2 operational configuration

First run `build_credential_provider.py` on the verified Linux host through
SSM with the committed `provider-build-inputs.json` on stdin. It builds a
candidate only: the service is not changed. Then run
`rke2_credential_provider.py` with private JSON on stdin
(`repository_url`, `digest`).
Recheck the expected account, instance/VPC and owner tags before sending it.
This is an explicit operator action, not a startup hook or CI deployment.

The tool requires one healthy node and at least 17 healthy system pods, and
refuses to restart while non-system pods exist. It backs up the primary RKE2
configuration privately, preserves it byte-for-byte, and creates only:

- `/var/lib/rancher/credentialprovider/bin/ecr-credential-provider`
- `/var/lib/rancher/credentialprovider/config.yaml`
- `/etc/rancher/rke2/config.yaml.d/90-bravo-ecr-credential-provider.yaml`

Existing different files, symlinks or foreign ownership are rejected. The
provider is reproduced from the official cloud-provider-aws v1.35.2 source
commit with the dependency-only fix recorded in `provider-dependencies.patch`.
The exact source archive, Go 1.26.10 SDK, dependency files and resulting binary
hashes are pinned in `provider-build-inputs.json`. The build uses a private
temporary directory and credential-free environment, runs upstream package
tests and requires byte equality with the independently built/scanned binary.
It obtains authentication from the instance role; the response
is checked in memory and discarded. No registry password is persisted or printed.
Only a changed installation restarts `rke2-server`, then requires readiness.
Only the exact previously installed official binary hash or the fixed hash is
accepted. A known old binary is backed up privately, then replaced atomically
in the same directory. An error restores its bytes and removes only new
configuration files before restarting the previous configuration. A repeat
matching invocation verifies without a build, download or restart.

`rke2_private_pull_probe.py` accepts the same private fields plus the reviewed
`platform_digest`. It creates a restricted, deny-all-network verification
namespace and two sequential `imagePullPolicy: Always` pods. Each checks the
runtime UID/app imports without calling a database, cloud SDK or object store.
The tool verifies successful pull events and runtime digests, then deletes
the owned probe pods. The empty namespace and deny-all policy remain for reuse.
It does not create an imagePullSecret or mount a service account token.

On 2026-11-03 both pods succeeded at the original digest, and final node 1/1
and system pods 17/17 were ready. Repeating installation made no changes or
restart. The direct provider response reported `cacheKeyType: Registry` and
`cacheDuration: 6h0m0s`; kubelet's provider flags were observed. A provider-call
metric was not exposed under the queried metric name, so its counts are null.
The upstream provider computes cache duration from half the remaining token
lifetime. Neither a six-hour cache expiry nor a twelve-hour token expiry was
awaited: **expiry-and-renewal was not observed**. See bounded evidence under
`docs/evidence/t7-private-registry-2026-11-03/` at repository root.

Provider sources: [official EKS installation](https://github.com/awslabs/amazon-eks-ami/blob/main/templates/al2023/provisioners/install-worker.sh),
[upstream cache logic](https://github.com/kubernetes/cloud-provider-aws/blob/v1.35.0/cmd/ecr-credential-provider/main.go),
[kubelet provider configuration](https://kubernetes.io/docs/tasks/administer-cluster/kubelet-credential-provider/).

## Provider dependency correction and reproducibility

The first single-file `trivy fs` attempt recognized **zero analyzers/packages**;
its exit zero was not evidence of a clean binary. The preserved corrected
`rootfs` scan of the official EKS 1.35.9 binary recognized 53 Go packages and
found four fixable HIGH findings: CVE-2026-46600 / CVE-2026-78669 in x/net,
CVE-2026-56851 / CVE-2026-56852 in x/text. The latest compatible upstream 1.35
release was v1.35.2, with no release binary assets and older dependencies.
The latest official EKS 1.35 binary available at verification was the same
vulnerable binary. No ignore or severity exception was added.

The patch requests x/net v0.60.0 and x/text v0.42.0. Their minimum module
requirements also raise crypto, mod, sync, sys, term, tools and the Go language
version; the exact closure is recorded in the patch and dependency hashes.
Credential acquisition, token handling and provider runtime source are unchanged.
Two independent builds produced the same linux/amd64 binary hash. Correct
rootfs scans recognized 54 Go packages and zero fixable HIGH/CRITICAL findings.
This scan does not claim the absence of all vulnerabilities or validate ECR
permissions; the real host authentication and kubelet pull are separate checks.

Run the scanner with an executable binary (0755), an empty scanner config and
ignore file, and positive analyzer/package assertions:

```sh
python3 scan_credential_provider.py "$REVIEWED_BINARY" --output "$PRIVATE_SCAN_REPORT"
```

The helper stages a temporary root filesystem, clears ambient `TRIVY_*`
settings, uses `--list-all-pkgs`, and rejects coverage zero even when Trivy
returns exit zero. Keep the raw report private and publish its bounded summary.
The original vulnerable binary is a useful negative control: the same command
must recognize its packages and return exit one with the four HIGH findings.
The former coverage-zero attempt and original receipts remain historical;
replacement/readiness/pull receipts supersede them only for their stated scope.

## Validation

```sh
terraform init -backend=false
terraform fmt -check -recursive
terraform validate
terraform test
trivy config --severity HIGH,CRITICAL --exit-code 1 .
```

The mock tests verify repository retention/encryption/scanning and exact
OIDC/publisher boundaries, including rejecting nonnumeric identity inputs.
Mock tests do not establish live AWS access or successful image delivery.

References: [ECR manifest formats](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-manifest-formats.html),
[ECR encryption](https://docs.aws.amazon.com/AmazonECR/latest/userguide/encryption-at-rest.html),
[ECR authentication](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html),
[GitHub OIDC in AWS](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws),
[EKS node role](https://docs.aws.amazon.com/eks/latest/userguide/create-node-role.html),
[RKE2 credential provider options](https://docs.rke2.io/reference/server_config).
