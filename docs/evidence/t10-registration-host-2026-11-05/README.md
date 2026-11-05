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
  were checked separately. Later renderer/docs and build-recipe pin changes do not change
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
| Actual registration | Source environment, target environment, app and plan: 201; same-key plan replay: 200 with the identical receipt; missing/invalid/wrong-audience/expired authentication: 403 |
| Committed storage | All four registration receipts match under the schema 3 runtime DB role and existing plan transaction |
| TaskCatalog | One signed credential-migration record read for the same tenant/plan digest; wrong tenant/digest/key and duplicate write rejected |
| Cleanup | Publisher Job, Pod and ConfigMap removed by original UID; owned TLS transport/forward/guard removed; signed catalog storage retained |
| T19 | Authentic new runs matching the registered identifiers are not received; no context or report created |

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

5. SSM Port connections failed with a missing-plugin error. The T7 owner
   reproduced inotify exhaustion and recovered the shared transport without an
   agent restart. A new RunCommand and TLS Port probe passed before registration
   resumed. This session terminated only its own sessions and hash/PID-bound
   forwards; it did not restart the agent or terminate another owner's session.
6. Initial CI rejected an untagged Dockerfile base argument and unpinned Git
   package. The versioned base input and exact package version fixed those
   findings. The corrected recipe passed a local image build, Git execution and
   Hadolint. The deployed image identity above remains the measured runtime.

## Registration and catalog acceptance

The authenticated HTTP path and actual publisher Job completed on November 5.
See [runtime-result.json](runtime-result.json) for the retained result projection
and hashes of private originals. The caller did not save authentication proofs.
The catalog writer used the existing runtime DB role, verified the four
committed receipts, held the current plan transaction, and checked the current
registration grant before signing. The plan remains COLLECT version 1.

Both collectors passed required reads and denied pod creation and registered
CNPG patch checks. The source EKS rules review did not establish the entire
write scope; its `write_scope_unknown` warning is retained. These two explicit
write denials must not be described as proof that every possible write is
forbidden. The target collector reported no warning.

Collector tokens were one-hour TokenRequests for this bounded registration
window. The successful initial registration does not establish token rotation
or a permanent operator ingress. Before another registration, renew the
protected references and re-check endpoint addresses, TLS and current grants.
The original registration records and signed catalog are retained for consumers.

## Remaining T19 and product acceptance

The T14/T19 owner agreed to produce new authentic runs from these registered
identifiers. Their producer originals must carry the actual environment/app
identity; historical IDs or times must not be rewritten. The target application
was not present in the observed inventory, so target app-to-database checks
remain unavailable. The existing GateRecords trusted-host port is the intake
boundary; this PR adds no public control-run HTTP enrollment.

No T19 context/report, product Admission, change approval, SHIFT or Executor
call was created. Existing owners retain ProductInputRecords, ProductExecution,
Coordinator assembly, T14 production and T19 evaluation. This completed
registration/catalog boundary is not the whole product E2E.
