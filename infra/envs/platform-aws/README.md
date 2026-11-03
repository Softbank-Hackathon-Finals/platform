# T7 private EC2 + RKE2 and constrained Actions OIDC

This stack implements the **platform headquarters** in design 7.2, not the on-premises migration target. Team main/develop both specify EC2 + RKE2 for T7; T6 is EKS and T2 is the separate on-premises RKE2. See [T7 #12](https://github.com/Softbank-Hackathon-Finals/platform/issues/12).

## State and ownership

T1 owns the platform VPC, private subnet, route table and source peering. This stack consumes those reviewed IDs and owns one outbound NAT route, its public egress subnet/IGW/NAT, a **private** EC2 host, SSM role, fixed probe document and GitHub OIDC probe role. It adds no inbound SG rules or public host IP. It never imports or destroys T1 resources. Use a separate encrypted S3 state key and lockfile; example input below contains placeholders only.

```sh
terraform init -backend-config=/private/path/platform.backend.json
terraform plan -var-file=/private/path/platform.tfvars.json -out=/private/path/platform.tfplan
terraform apply /private/path/platform.tfplan
```

The private input supplies the reviewed account/region, VPC/subnet/route table, exact Ubuntu 24.04 AMI, existing GitHub OIDC provider ARN and exact `github_oidc_subjects`. `allowed_account_ids` rejects another account. The private subnet must belong to that VPC and have public-IP assignment disabled. No tfvars, backend values, plan/state, resource IDs or kubeconfig belong in Git.

The NAT subnet defaults to a previously unused /24 inside the platform VPC. Check for overlap and an existing default route/IGW **before** planning. The ownership assumption is one runtime stack per T1 environment. Existing egress must be deliberately imported or reused by a reviewed change, not overwritten. NAT is single-AZ for the demo; this is not an HA deployment.

## Bootstrap and operator access

`infra/shared/rke2/bootstrap.sh` is the **T2/T7 common bootstrap** tested on this AWS host. T2 uses it through [`infra/envs/onprem`](../onprem) and has run it on a local VM; the demo machine reinstall is still pending. Changing this file changes this host's user data and replaces the instance on the next apply. It pins RKE2 `v1.35.9+rke2r1`, enables CIS, creates the local etcd identity, applies upstream CIS sysctls before startup, disables sleep and ingress controllers, and keeps the admin kubeconfig mode 0600. Re-running against an active server fails before replacing its configuration. It is not an upgrade tool or a full CIS benchmark result.

The AWS wrapper only prepares HTTPS package access and the isolated smoke namespace/RBAC. Operators use their authorized AWS SSM session. The RKE2 admin kubeconfig remains on the host and is not a shared team credential. Individual RKE2 authentication/expiration/revocation and team distribution are still pending. IMDSv2 hop limit 1 and a host-role secret-read denial are defense layers; they do not replace T3 pod isolation tests or its tool-layer authorization.

## OIDC smoke boundary

The live repository has immutable-ID OIDC subjects. Obtain the prefix with `gh api repos/Softbank-Hackathon-Finals/platform/actions/oidc/customization/sub`; use exact develop or this validation branch subject strings. Trust requires both exact `sub` and `aud=sts.amazonaws.com`. Forks, pull-request subjects and wildcard subjects are not accepted. The provider is an existing T1/account object, not recreated here. [GitHub AWS OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws).

`runtime-smoke.yml` is opt-in (`BRAVO_RUNTIME_SMOKE_ENABLED=true`), manually dispatchable after merging to the default branch, and can run on this explicit validation branch when its workflow changes. It does not run on ordinary PRs. The role/instance identifiers are GitHub Secrets `BRAVO_RUNTIME_SMOKE_ROLE_ARN` and `BRAVO_RUNTIME_SMOKE_INSTANCE_ID`; no AWS access key is stored there.

Actions receives a 900-second session with only fixed-document `ssm:SendCommand` against this one instance and `ssm:GetCommandInvocation`. The document accepts **no arguments, shell commands or manifests**. It checks Ready, deploys a fixed ConfigMap through an impersonated namespace-only service account, reads it back, and checks Secret/other-namespace denials. A separate Actions step proves arbitrary `AWS-RunShellScript` invocation is denied. Document updates and IAM changes remain outside that role. This is a control-path smoke, not a production deployment agent.

## Validation and remaining T7 work

See [live evidence](../../../docs/evidence/t7-runtime-2026-11-03/README.md). Local checks: Terraform fmt/validate, four mocked policy/subnet tests, bash syntax, actionlint and Trivy HIGH/CRITICAL. Live AWS: private EC2, RKE2 Ready and namespace authorization. Live Actions: OIDC → fixed SSM document → RKE2 ConfigMap plus arbitrary-shell denial.

Still separate: platform DB, production application deployment, GHCR image digest publication/pull, collector versus Executor AWS/Kubernetes roles, T2 demo machine reinstall, operator SSO and individual RKE2 access. The workload verification session owns those follow-ups in separate state; it must reuse this host and preserve these Terraform resources.

## Cost and teardown

EC2, EBS, NAT, NAT IPv4/data and logs incur charges while retained. `component=platform` is applied to taggable resources and the EBS volume; t3 CPU credits use standard mode. The AWS account budget is an alert, not an automatic spending cap. Do not leave this stack running without the runtime owner and a dated review. Current retention is for the coordinated workload integration; teardown is deferred while another session uses the cluster. Before teardown, reconcile owners, preserve required DB/state/evidence, disable `BRAVO_RUNTIME_SMOKE_ENABLED`, and destroy only this state. Never destroy the shared T1 state. The generic default RKE2 snapshot/local storage is not an off-host backup.

References: [RKE2 CIS guide](https://docs.rke2.io/security/hardening_guide), [RKE2 installation](https://docs.rke2.io/install/methods).

### Retained runtime cost baseline

On 2026-11-03 AWS Price List API in Seoul reports Linux shared-tenancy t3.large at US$0.104/hour and zonal NAT at US$0.059/hour. With **two t3.large nodes, two NAT gateways, one standard-support EKS control plane and two NAT public IPv4 addresses**, the combined T6+T7 baseline is `(2×0.104 + 2×0.059 + 0.10 + 2×0.005) × 730 = US$318.28/month` if all run continuously. This is a planning subtotal, not the bill or an all-in forecast. EBS/CSI volumes, KMS, logs, data transfer/NAT processing, additional nodes, CPU surplus and unrelated existing account resources are extra. The 500,000 KRW account budget (US$372.32 at its recorded conversion) therefore needs review as workload resources grow. No automatic teardown is scheduled while integration is active.

Pricing references: [EKS standard support](https://aws.amazon.com/eks/pricing/), [NAT and public IPv4](https://aws.amazon.com/vpc/pricing/). Keep current per-region Price List evidence privately with the operator receipts.
