# Security Review

This chart deploys the read-only Security Review endpoint used as an additional fail-closed condition by the
T15 approval host. It never receives an Executor credential and never authorizes execution.

## Required operator inputs

Create these outside Git before enabling `securityReview` in the Argo CD bootstrap values.

| Input | Required key or meaning |
|---|---|
| `security-review-tls` Secret | `tls.crt`, `tls.key`; SAN `security-review.platform-state.svc` |
| database Secret | `dsn`; dedicated SELECT-only reader using verify-full TLS |
| grants ConfigMap | `grants.json`; exact caller SA UID, scope, `enforce`, and expiry |
| API CA ConfigMap | `ca.crt`; platform Kubernetes API trust root |
| audit storage | pre-provisioned `platform-local` volume from `infra/envs/platform-security-review` |
| registry Secret | private GHCR pull credential |

Apply `infra/envs/rehearsal-access/security-review-tokenreview-rbac.yaml` once. The binding grants only
`tokenreviews.create` to `platform-state/security-review`.

Set the Kubernetes API backend address/port and database endpoint explicitly. NetworkPolicy is evaluated after
Service DNAT on the platform Calico path, so `tokenReview.apiAddress` and `apiPort` must identify the current API
endpoint rather than the `kubernetes` ClusterIP. The chart renders only DNS, that API `/32`, and the registered
database `/32` as egress. The caller ingress and T15 egress remain in
`infra/envs/rehearsal-access/rehearsal-host-network.yaml`.

## Enable and accept

1. Merge the generated immutable `platform-security-review` image pin PR.
2. Prepare the database schema, restrictive RLS, reader, current registrations, signed evidence and grants.
3. Create TLS, database and API CA mounts outside Git.
4. Provision the retained audit volume, then apply TokenReview RBAC and the rehearsal NetworkPolicies.
5. Enable the GitOps application with protected runtime values.
6. Verify TokenReview success, wrong-audience denial, valid TLS, wrong-CA denial, allowed ingress and blocked caller.
7. Only then merge the current T15 image pin and run `operations/security-program/run.py warm`.

The 270-second demo budget does not weaken these checks. Pre-warming is bounded to 45 seconds and any missing
service, token, CA, database registration or evidence remains HOLD.
