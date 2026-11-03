# T7 RKE2 / OIDC foundation — 2026-11-03

[Actual GitHub Actions run](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41602534226) succeeded on `ca52cc7d129e8808fff9e32951d346763b1f1452`. The sole later AWS change was an in-place EC2 Name tag for console identification; the runtime bootstrap, role trust, permissions and SSM document were unchanged.

| Check | Result |
|---|---|
| Terraform fmt/init/validate | PASS |
| Mock boundary tests | 4 PASS |
| bash syntax / actionlint / Trivy HIGH,CRITICAL | PASS / PASS / 0 stack findings |
| EC2 | private Ubuntu 24.04; no public IPv4, no inbound SG rule; SSM Online |
| RKE2 | v1.35.9+rke2r1, 1/1 Ready; CIS configuration, admin kubeconfig 0600 |
| Local SSM probe | Ready, fixed ConfigMap deploy/readback, Secret and other-namespace denial PASS |
| Actions authentication | real GitHub OIDC; STS temporary session 900 seconds |
| Actions deployment | fixed SSM document → namespace-limited ConfigMap deploy/readback PASS |
| Arbitrary shell document | AWS-RunShellScript denied by IAM |
| SSO / individual team RKE2 access | NOT VERIFIED / not distributed |
| Full CIS benchmark / app image rollout / platform DB | not claimed |

[Machine-readable results](results.json) bind the source file hashes and run. The Actions proof is a fixed namespace control-path deployment, not demo-chat, GHCR publication, a general CI agent or a completed T7. Secret/account/resource values stay in private operator inputs and GitHub Secrets. The ordinary PR CI has no cloud mutation. The existing workload session owns subsequent image/DB integration; the cluster is retained while that work runs.
