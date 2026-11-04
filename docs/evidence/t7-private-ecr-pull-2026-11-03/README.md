# T7 private ECR host permission — 2026-11-03

The organization package policy remains unchanged. A separate registry stack owns the private ECR repository and publisher OIDC role. This change connects the existing RKE2 EC2 role to that exact repository in the same account/region.

## Measured scope

- Local: Terraform format/validate, 9 mocked tests, Trivy HIGH/CRITICAL 0, credential and public evidence checks.
- Actual AWS plan/apply: one IAM inline policy created; no EC2, bootstrap, network, existing OIDC or EKS changes.
- Live IAM role readback and principal-policy simulation: 15 action evaluations; selected repository reads and registry authentication allowed; other-repository reads, push/delete and existing secret operations denied.
- AWS Access Analyzer: no findings on the deployed policy.

These are IAM configuration and simulation results. Actual image pull, digest equality and persistent RKE2 credential refresh are separate workload validations. The existing EKS AWS-managed pull policy remains broader and was not narrowed in this change.

`results.json` records the source commit and file hashes used for the policy apply. Raw state, plan, policy/readback identifiers and private repository inputs remain in operator storage. No registry token or IAM access key is stored by this module.

## Reproduction and lifecycle

Set `private_ecr_repository_name` in the reviewed platform input, then plan against the existing platform backend. Confirm that only the intended host policy changes before applying. Use IAM `simulate-principal-policy` against the deployed host role to evaluate selected/other repository reads, upload/delete and authorization. Validate the deployed policy with Access Analyzer. Runtime owners perform a fresh digest-pinned pull using host credentials separately.

Setting the input back to `null` removes only this inline policy; it does not delete the repository, its publisher role, any cached image, or the EC2 host. Coordinate this revocation with runtime owners.
