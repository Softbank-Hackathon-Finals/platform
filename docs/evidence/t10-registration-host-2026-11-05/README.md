# T10 registration host runtime evidence

This is an operational follow-up to the accepted registration contract and PR
[#307](https://github.com/Softbank-Hackathon-Finals/platform/pull/307). The original
T7/T10 mock completion criteria are unchanged. This record does not claim the
T19 two-environment gate or product happy path is complete.

## Accepted source and bounded deployment

- Registration SSO merge: `7020d334d7eaa39620a9570b6fc5b1d326b7b7cc`.
- Runtime source: `be0390122bf7d5ec99778a6b82f2a3846dc9cbb4`.
- Imported image: `sha256:95516b7a90ea873d4b48d2e4ae0be0f3c9ab2c69964a795ee33a15cbd0b79772`.
- OCI archive: `fda7703be86abd90c606d35e4b946a25b533b32654d9ba4afcd779c45c73767a`.
- The source build, import, platform manifest/config identity and running image
  were checked separately. Later renderer/docs formatting changes do not change
  that runtime image. Private execution journals retain the exact manifests,
  source hashes, provider command IDs and resource UIDs.

The host is separate from window intake and product read. It uses the existing
schema 3 runtime DB role. It never mounts a migration credential, human AWS
credential, Kubernetes admin credential or database CA private key. Dedicated
collector TokenRequests are transferred encrypted into an immutable Secret;
the one-use decryption Secret is removed after recipient readback. TLS keys are
generated in memory on the platform and stored only in their designated Secret.

## Confirmed measurements

| Boundary | Observation |
|---|---|
| Personal SSO to actual AWS STS | Current principal and tenant accepted; wrong audience and expired proof rejected; 5 checks passed locally against actual STS |
| Source and target inventory | Actual CNPG/collector identities read; source app image, ReplicaSet and approved Git revision agree |
| Target application | CNPG, PgBouncer and object store observed; no demo-chat Deployment in that inventory |
| Local implementation | 69 relevant tests passed; Ruff and public text checks passed |
| Image | Linux amd64, UID/GID 10001, canonical imports and Git verified; imported digest/config matched |
| Platform deployment | One Ready registration Pod, zero restarts; approved source checkout init exited 0 |
| Actual registration | Pending authenticated HTTP execution; do not infer it from Pod readiness |
| TaskCatalog and T19 | Pending actual registration receipts and authentic matching original runs |

## Failures preserved and corrected

1. The first image receiver expired without importing. Its owned directory,
   process and listener were removed after receipt verification. A later image
   transfer/import passed and its SSM session, temporary archive and receiver
   were removed. No existing workload image was replaced.
2. Several SSM commands failed with a provider document-worker IPC timeout.
   Failed submissions were not promoted to execution success. Before retrying
   a creation, the operator read exact resource absence/UIDs. The encrypted
   collector payload was reused only after recipient absence and the original
   transfer-key UID were confirmed.
3. AWS returned an EKS hostname with uppercase characters. Kubernetes rejected
   it in `hostAliases` before deployment. The renderer now normalizes alias
   hostnames to lowercase; a regression test covers this input.
4. `kubectl create -f List -o json` emits adjacent JSON documents. The private
   execution wrapper initially treated a successful dry run as one JSON value.
   The wrapper now checks returned object names and reads each created UID.
   The dry-run failures made no registration objects or database rows.

The actual platform CNI is Canal. The deployment uses a standard NetworkPolicy
with observed exact addresses and matching host aliases for this window,
retaining TLS hostname verification. It does not install a Cilium-only policy
or borrow rehearsal/apply/broker network labels. The dedicated collector route
is owned and measured in [#317](https://github.com/Softbank-Hackathon-Finals/platform/pull/317).

## Remaining acceptance

Run actual SSO through the TLS registration endpoint, verify environment/app/
plan receipts and same-key replay, then read the committed rows. Publish and
read the signed TaskCatalog only for that actual plan. T19 originals must carry
the registered environment/app identity; historical originals must not be
rewritten to fit a new plan. Missing target application/control evidence remains
unknown. No gate PASS, change approval, SHIFT or Executor call is implied here.
