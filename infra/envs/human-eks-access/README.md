# T1 human SSO access to an existing EKS cluster

This separate stack owns one `STANDARD` EKS access entry for an explicitly
reviewed, existing `AWSReservedSSO_` IAM role and one cluster-scoped
`AmazonEKSClusterAdminPolicy` association. This is the approved **human
development administrator** scope; do not use it for agents, CI, nodes, or
application service accounts. The entry carries `component=access` plus the
repository's project and management tags. EKS policy associations do not
support tags.

The access entry is bound to the permission-set IAM role, not an individual
person. Every user assigned that permission set in this account receives the
same EKS policy. Review the actual account assignments and keep their scope
limited to the authorized development operators. Each operator must still
authenticate with their own SSO identity for session attribution.

The stack reads the existing cluster and IAM role. It does not own the cluster,
endpoint, authentication configuration, network, nodes, IAM roles, workloads,
Organizations, Identity Center instance, users, assignments, or permission sets.
In particular, leave the `source-eks` state and its bootstrap operator entry
unchanged. Registering the additional SSO role does not retire that operator.

## Required private inputs

All four inputs are required; none has a default.

| Input | Review boundary |
|---|---|
| `account_id` | Exact account containing the cluster and the existing SSO IAM role. The provider also restricts the authenticated account with `allowed_account_ids`. |
| `region` | Explicit EKS region, checked against the returned cluster ARN. No implicit Seoul or Japan selection. |
| `cluster_name` | One existing `ACTIVE` cluster with `API` authentication and a private-only endpoint. A mismatch stops planning; it does not reconfigure the cluster. |
| `sso_role_arn` | Exact same-account IAM role ARN under `aws-reserved/sso.amazonaws.com/` with an `AWSReservedSSO_` name and its full suffix. IAM must return the same ARN. Users, STS sessions, ordinary IAM roles, and wildcards are rejected. |

The Identity Center home region in the role path is independent of the EKS
region. AWS omits that path segment for an Identity Center instance in
`us-east-1`; both documented forms are accepted. Obtain the role ARN from IAM,
not by converting the human's STS session ARN. [AWS role ARN formats](https://docs.aws.amazon.com/singlesignon/latest/userguide/referencingpermissionsets.html)

Current Seoul development and a possible future Japan deployment are separate
scopes. A later regional deployment needs its own reviewed cluster and private
backend inputs; this stack neither chooses nor migrates to that destination.

## State ownership and reconciliation

Use the existing approved encrypted S3 state backend with **a distinct key**
for this account/region/cluster/role tuple, for example
`access/human-eks/SELECTED_REGION/SELECTED_CLUSTER/SELECTED_ROLE.tfstate`.
Never initialize this directory against the foundation, `source-eks`, workload,
or another access stack's key. The private backend configuration must include
the approved bucket, backend region, `encrypt=true`, KMS key, `use_lockfile=true`,
and the exact backend `allowed_account_ids`. The backend region locates its S3
bucket and need not equal the EKS region. Keep backend configuration, inputs,
plans, state, and raw readbacks in access-restricted private storage outside Git.
Sensitive input marking does not redact every provider data field. Plan output,
state, and saved plans contain identifiers and the existing cluster data.

Before creating anything, the runtime owner must inspect the exact cluster's
access entry for this role and its associated policies, and check existing state
ownership. An existing entry must be reconciled, not recreated blindly. If it
already belongs to another state, coordinate ownership before importing; two
states must never manage the same entry. Preserve unrelated entries and
associations. Compare any existing custom username/groups and extra policies
with the approved human scope before proceeding.

From this directory, after privately supplying the reviewed inputs:

```sh
terraform init -backend-config=/private/path/human-eks.backend.hcl
```

If the matching entry already exists and is not owned by another state, import
it using the private cluster/ARN values. Import the association only if that
exact policy is already attached. These commands update this stack's state;
they do not establish that a human can log in. The shell variables below must
come from the same reviewed private inputs, without command tracing.
`EKS_POLICY_ARN` must be the AWS-managed EKS access policy ARN for
`AmazonEKSClusterAdminPolicy`, as returned by `eks list-access-policies`.

```sh
terraform import -var-file=/private/path/human-eks.tfvars.json \
  aws_eks_access_entry.human "${EKS_CLUSTER}:${SSO_ROLE_ARN}"
terraform import -var-file=/private/path/human-eks.tfvars.json \
  aws_eks_access_policy_association.human \
  "${EKS_CLUSTER}#${SSO_ROLE_ARN}#${EKS_POLICY_ARN}"
terraform plan -var-file=/private/path/human-eks.tfvars.json \
  -out=/private/path/human-eks.tfplan
```

For a new entry, skip imports and create a saved plan. Review it before a
separately authorized application: it must contain only this human access entry
and its policy association, with no unexpected replacement or removal.
After reconciliation, read both AWS objects back and confirm a fresh plan has
no unintended changes. [Provider entry import](https://github.com/hashicorp/terraform-provider-aws/blob/v6.71.0/website/docs/r/eks_access_entry.html.markdown),
[association import](https://github.com/hashicorp/terraform-provider-aws/blob/v6.71.0/website/docs/r/eks_access_policy_association.html.markdown)

Deleting all account assignments to an Identity Center permission set can
delete its IAM role; a later assignment can produce a different suffix. Keep
the required assignment and coordinate any replacement instead of using a
wildcard. Even a recreated IAM principal with the same ARN can leave an EKS
entry unusable because EKS retains the original principal ID. Exact ARN checks
alone cannot detect that historical binding; role replacement needs explicit
entry reconciliation and a fresh human access test. [Identity Center role lifecycle](https://docs.aws.amazon.com/singlesignon/latest/userguide/referencingpermissionsets.html),
[EKS principal lifecycle](https://docs.aws.amazon.com/eks/latest/userguide/creating-access-entries.html)

## Human verification is separate

IAM role discovery and successful EKS registration prove configuration only.
Each person must authenticate through their own SSO profile, verify the intended
account/role, and use their own locally generated kubeconfig. Do not distribute
a shared administrator kubeconfig, static token, long-term key, or client
certificate. Access to the existing private network path is still required;
this stack does not open a public endpoint or create a tunnel.

The runtime owner must then observe actual Kubernetes authorization with that
human's SSO identity and the original TLS verification intact. Preserve private
evidence of the selected identity, cluster, and successful operation. Do not
substitute bootstrap operator credentials or impersonation. `kubectl auth can-i
--list` does not enumerate permissions provided by EKS access policies; it is
insufficient as the only verification. [AWS access policy verification caveats](https://docs.aws.amazon.com/eks/latest/userguide/access-policies.html)

## Local checks

```sh
terraform fmt -check -recursive
terraform init -backend=false -input=false -lockfile=readonly
terraform validate
terraform test -no-color
```

Tests use only mocked AWS data and synthetic account/cluster/role fixtures.
They cover the valid association, wrong caller/cluster/role account, non-SSO
principals, session/wildcard ARNs, IAM path mismatch, region mismatch, inactive
cluster, unsupported authentication mode, public endpoint, and the independent
Identity Center region/path forms. They do not call AWS or Kubernetes and do
not prove real login, network reachability, runtime authorization, or T1 completion.
