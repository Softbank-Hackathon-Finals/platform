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

Run `rke2_credential_provider.py` on the verified host through the existing
operator SSM path with private JSON on stdin (`repository_url`, `digest`).
Recheck the expected account, instance/VPC and owner tags before sending it.
This is an explicit operator action, not a startup hook or CI deployment.

The tool requires one healthy node and at least 17 healthy system pods, and
refuses to restart while non-system pods exist. It backs up the primary RKE2
configuration privately, preserves it byte-for-byte, and creates only:

- `/var/lib/rancher/credentialprovider/bin/ecr-credential-provider`
- `/var/lib/rancher/credentialprovider/config.yaml`
- `/etc/rancher/rke2/config.yaml.d/90-bravo-ecr-credential-provider.yaml`

Existing different files, symlinks or foreign ownership are rejected. The
provider is the official EKS 1.35.9 amd64 static binary, checked against its
upstream SHA-256. It obtains authentication from the instance role; the response
is checked in memory and discarded. No registry password is persisted or printed.
Only a changed installation restarts `rke2-server`, then requires readiness.
An error rolls back only files created by that invocation and restarts the
previous configuration. A repeat matching invocation verifies without restart.

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
