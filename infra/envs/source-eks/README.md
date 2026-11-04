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

## Network policy enforcement

The VPC CNI add-on explicitly sets `enableNetworkPolicy` to the string `"true"`
through `configuration_values`. The version remains the reviewed input and
the CNI retains its dedicated IRSA role. Updates use
`resolve_conflicts_on_update = "PRESERVE"` for existing managed-field conflicts;
this is not a substitute for reading and preserving the add-on's current
version, role and any additional configuration when coordinating a live update.
See the [AWS enablement procedure](https://docs.aws.amazon.com/eks/latest/userguide/cni-network-policy-configure.html).

For a live `UpdateAddon` request, explicitly include the currently verified
dedicated CNI `serviceAccountRoleArn`; with the CLI, pass
`--service-account-role-arn "$CURRENT_CNI_ROLE_ARN"`. Preserve the reviewed
version and other configuration in the same request. After reconciliation,
compare the returned role ARN with the `eks.amazonaws.com/role-arn` annotation
on `kube-system/aws-node`. Verify that each newly created CNI Pod has the
expected `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` injection and becomes
Ready without CNI IAM errors. Keep actual role values private and do not read
or print the web identity token. The add-on ARN field alone is insufficient
evidence that the running CNI uses that identity.

During one observed activation, the first update request omitted the existing
role ARN. The in-progress add-on still reported that role, but the service
account annotation was absent and new CNI Pods lacked the web identity
environment. They failed readiness with `MissingIAM` for
`ec2:DescribeNetworkInterfaces`; the previous Pod had the dedicated-role
environment. Once that update completed, its add-on role field was also absent.
This matches the [UpdateAddon API contract](https://docs.aws.amazon.com/eks/latest/APIReference/API_UpdateAddon.html#API_UpdateAddon_RequestBody):
omitting this role selects the node's IAM permissions. `PRESERVE` did not supply
the missing request field. Restoring the original service-account annotation,
replacing only the affected CNI Pod with a UID precondition, and explicitly
resubmitting the same dedicated role restored ACTIVE/Ready with identity
agreement at all three layers. No IAM permissions were broadened. Readiness
and identity recovery remain distinct from actual traffic-denial verification.

Creating a NetworkPolicy or seeing a healthy nodeagent container is insufficient.
The pinned VPC CNI chart defaults the feature to
[`"false"`](https://github.com/aws/amazon-vpc-cni-k8s/blob/7bb32b54d7c56f3ee3c5a63bddf97c4af49ea8ec/charts/aws-vpc-cni/values.yaml#L124-L126),
and nodeagent 1.4.3 initializes the policy controllers and eBPF client only when
[enforcement is enabled](https://github.com/aws/aws-network-policy-agent/blob/6b9c41cc2a373ad990faca10173cda64d4aace41/main.go#L107-L133).
Before treating a policy as enforced, verify the live add-on configuration,
the nodeagent's `--enable-network-policy=true` argument, and generated
`PolicyEndpoint` objects that select the intended Pods and resolve the intended
caller addresses and ports. Inspect reconciliation errors and eBPF attachment
when these observations disagree; do not edit the managed PolicyEndpoints.

After reconciliation, test fresh connections from the actual permitted
workload and from callers with the wrong label and wrong namespace. Record
the current policy and Pod identities with those results. For the source pooler
route, the real PgBouncer must still establish its verified TLS DB connection
while unauthorized callers cannot use the proxy's business ports. Standard
startup mode initially allows traffic until policies are programmed; this
change does not switch the entire cluster to strict mode. Neither mocked
Terraform tests nor an enabled argument prove runtime denial.

## Logs, validation and ownership

All five EKS control-plane log types are enabled. CloudWatch audit logs and Kubernetes Secrets use a rotating CMK. Retention is seven days for the demo. CloudTrail management/data-event logging and DB pgAudit are separate follow-ups; EKS audit does not substitute for them.

The operator validation uses SSM remote-port forwarding through the private platform host, the EKS CA and the original TLS server name; no TLS verification bypass is needed. Kubeconfig and actual endpoint stay in a private directory. Temporary test namespaces use unique names and are removed after the test. See [live evidence](../../../docs/evidence/t6-runtime-2026-11-03/README.md).

Local: Terraform fmt/validate, three mocked boundary tests, Trivy HIGH/CRITICAL. Cloud: EKS/nodegroup ACTIVE, node Ready, `/readyz`, actual namespace authorization and audit canary. These do not prove demo-chat/DB or cross-cloud cutover.

The cluster is retained while the separate workload session performs integration. Before destroying this state, coordinate with that owner, remove dependent workloads/CSI volumes/IRSA state in the documented order, preserve required data/evidence and confirm the intended environment. Never destroy T1 to clean up T6. EKS control-plane, node, NAT, IPv4, EBS and log charges remain until their resources are actually removed; the budget alert does not stop them automatically.

References: [EKS managed nodes](https://docs.aws.amazon.com/eks/latest/userguide/create-managed-node-group.html), [access entries](https://docs.aws.amazon.com/eks/latest/userguide/access-entries.html), [control-plane logs](https://docs.aws.amazon.com/eks/latest/userguide/control-plane-logs.html).
