# T6 private EKS foundation

This stack prepares the **source** EKS defined in design 7.2 and [T6 #11](https://github.com/Softbank-Hackathon-Finals/platform/issues/11). The platform headquarters is a separate EC2/RKE2 stack (T7). This PR does not mark T6 complete: CNPG/PgBouncer/ESO, demo-chat, workload roles, S3/Secrets Manager, DB/CloudTrail canaries and data migration are separate work.

## Inputs and lifecycle

Consume the reviewed T1 `foundation` output: source VPC, two private subnets in different AZs, its route table, platform CIDR and target account/region. This state owns only the EKS foundation and its outbound NAT/IGW/egress subnet/default route. Preserve T1 VPCs, peering, routes, SGs and log state. Source and platform stack lifecycles are independent; each owns its own NAT because peering is not a shared internet gateway.

```sh
terraform init -backend-config=/private/path/source.backend.json
terraform plan -var-file=/private/path/source.tfvars.json -out=/private/path/source.tfplan
terraform apply /private/path/source.tfplan
```

Private input also supplies an exact AL2023 EKS release, vpc-cni addon version and stable individual IAM principal ARN. Fetch supported versions from AWS for the selected region/Kubernetes version and record them privately before planning. Current live validation: EKS 1.35, AL2023 `1.35.9-20261031`, vpc-cni `v1.23.2-eksbuild.8`. The provider is locked to AWS 6.71.0. AWS account allowlisting, two distinct AZ checks and same-account principal validation run before creation. Ensure no overlapping egress subnet, existing IGW or existing default route before applying; existing objects require an explicit reviewed reuse/import plan.

## Access and IAM

- Private-only Kubernetes API. The platform CIDR may reach TCP443 over T1 peering; no public API endpoint or SSH is configured. API reachability does not grant Kubernetes authorization.
- EKS `API` authentication with an explicit individual access entry. The creator receives no implicit admin access. The bootstrap operator currently has cluster-admin to install/test the foundation; team roles and namespace scopes are not provisioned. A stable SSO role ARN can replace the operator input after real SSO validation. An STS session ARN or another-account principal is rejected.
- The 11/3 live bootstrap used the existing personal IAM operator, **not SSO**. Console federation is also not SSO proof. Operator access and application workload identity are separate.
- VPC CNI uses its own exact-service-account IRSA role. Nodes get worker/ECR-pull policies, not the CNI policy. Node IMDS requires v2 with hop limit 1 and the EBS root volume is encrypted.
- The EKS OIDC provider is owned here. Downstream workload states must reuse its issuer/provider; they must not create a duplicate provider. The workload session owns EBS CSI, ESO/demo-chat IAM, Secrets Manager/S3/KMS and Kubernetes workloads in a separate state.

One managed node starts by default; max size 2 is a limit, not an autoscaler installation or HA guarantee. EKS manages its cluster SG. This foundation does not claim workload egress isolation or T3/T14 pass results. Kubernetes network policies and app permissions require their own real deny tests.

## Logs, validation and ownership

All five EKS control-plane log types are enabled. CloudWatch audit logs and Kubernetes Secrets use a rotating CMK. Retention is seven days for the demo. CloudTrail management/data-event logging and DB pgAudit are separate follow-ups; EKS audit does not substitute for them.

The operator validation uses SSM remote-port forwarding through the private platform host, the EKS CA and the original TLS server name; no TLS verification bypass is needed. Kubeconfig and actual endpoint stay in a private directory. Temporary test namespaces use unique names and are removed after the test. See [live evidence](../../../docs/evidence/t6-runtime-2026-11-03/README.md).

Local: Terraform fmt/validate, three mocked boundary tests, Trivy HIGH/CRITICAL. Cloud: EKS/nodegroup ACTIVE, node Ready, `/readyz`, actual namespace authorization and audit canary. These do not prove demo-chat/DB or cross-cloud cutover.

The cluster is retained while the separate workload session performs integration. Before destroying this state, coordinate with that owner, remove dependent workloads/CSI volumes/IRSA state in the documented order, preserve required data/evidence and confirm the intended environment. Never destroy T1 to clean up T6. EKS control-plane, node, NAT, IPv4, EBS and log charges remain until their resources are actually removed; the budget alert does not stop them automatically.

References: [EKS managed nodes](https://docs.aws.amazon.com/eks/latest/userguide/create-managed-node-group.html), [access entries](https://docs.aws.amazon.com/eks/latest/userguide/access-entries.html), [control-plane logs](https://docs.aws.amazon.com/eks/latest/userguide/control-plane-logs.html).
