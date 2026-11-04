# T8 / T30 development replication and destination preparation

This run pairs the existing source EKS environment with a separate local
x86_64 QEMU/TCG development VM. It verifies logical replication, attachment
copy and a source-backed destination PgBouncer. It does **not** establish full
destination application startup or production cutover readiness.

All times below are UTC on 2026-11-03. [readback.json](readback.json) is a curated
operator summary, not raw provider output. Private receipts retain cluster and
Pod identities, source files and manifests; `source_receipts` binds those
original bytes by SHA-256. No credential, object key, object body, account,
live endpoint or private contract is included here. Published route names are
functional descriptions. The public-identifier sanitizer found no remaining
matching identifiers in the curated summary.

## Actual observations

| Scope | Observed result | Limit |
| --- | --- | --- |
| Target database | CNPG 1.30.1 / PostgreSQL 18.4; migration precedes subscriptions; role/TLS probe 11/11 | One development instance, no HA or backup proof |
| Cross-cluster access, 11:45–11:46 | Replication paths 9/9 each; pooler path 5/5; verify-full and role checks, expected authentication/plaintext/hostname rejection | This earlier pooler-route probe is separate from actual PgBouncer below |
| Initial data | Both databases contain 2 messages and 1 attachment with equal full-row hashes | Small retained development fixture |
| Replication, 12:11–12:14 | 6/6: both directions, no duplicates, disabled-forward pause, reconnect catch-up, baseline restoration, healthy slots/errors | Bounded synthetic SQL, not production write fencing |
| Object storage, 12:09 and 12:14 | Signed write/read/hash and rejection checks 6/6, repeated after owned deployment restart with the same object hash | Isolated HTTP S3 service; T9 TLS/OpenBao/ESO remains separate |
| Attachment copy, 12:35:51 | 2 objects on each side, 119 bytes verified, 1 DB reference + 1 explicitly approved existing readiness canary; source writes 0 | One-way quiescent copy, not continuous replication |
| Destination pooler access, 12:28:06 | Limited Executor/Collector checks 26/26; exact pooler Pod and config data unchanged | Pooler-only; destination app access and cutover-ready both false |
| Actual PgBouncer, 12:41:35 | 10/10: client verify-full, source backend TLS/application role, original rows, INSERT then ROLLBACK without residue, admin settings, wrong-password/hostname rejection and subsequent positive connection | No business write flag changed; not app startup |
| Final read-only check, 13:16:37–13:17:14 | 20/20: cluster identities, nodes/workloads ready, original hashes preserved, valid slots/errors 0, source app blocked and healthy, target app NOLOGIN, pooler UID matches grant | Snapshot, not an unattended availability guarantee |

The final normal state is forward subscription **enabled**, reverse subscription
**disabled**, source API business writes blocked, and destination `chat_app`
NOLOGIN in both SQL and CNPG managed-role configuration. The source database
application role remains LOGIN for the pre-cutover pooler connection. API write
blocking therefore must not be described as complete SQL fencing. Synthetic
replication rows were removed on both sides; cleanup and manual restoration
were not pending. Sequences only advance; logical replication does not copy
sequence state.

The observed replication upper bounds were 9.123 seconds forward, 5.299 seconds
reverse and 7.366 seconds for reconnect catch-up. The pooler probe observed
0.985-second connection and 0.225-second rollback-write durations. These are
single development observations including remote calls and emulation, **not
p95, the final demo workload or an SLO result**.

Three role-specific Tailscale routes each returned three discovery pongs through
DERP `tok`; all three actual `tailscale0` interfaces reported MTU 1280. No direct
peer address was present in that observation window. These nine pongs do not
measure database latency or establish a PMTU/stress result. Network policy and
node settings were unchanged by this readback.

## Executed source and preserved failures

| Operation | Source bound to execution |
| --- | --- |
| Replication canary | `b3af8f2379b950e84b48fae85235ec6cd4baab00`; operator and module hashes in `replication_binding` |
| Attachment copy | `09f60a18c94e94fc43808fddf348da21191d0d91`; module/operator/RPC hashes in `attachment_binding`; RPC bytes frozen before calls |
| Target RBAC verification | PR #114 `65797a1bcc60f71b51d2f7c2d5598914f95790ea`; renderer/verifier/manifest/config hashes in `access_binding` |
| Actual pooler probe | `19b31c827d2344d3c7a94690397025b55ccd8b7a`; manifest hash in `pooler_binding` |
| Private image | demo-chat `ce5727eb3e3d2355077afdaadcb1a722aba9c7cd`; original OCI index and amd64 child in `fixed_app` |

The publication commit only splits one semicolon statement in the pooler probe
for Ruff and adds documentation/evidence. It is not substituted for the source
of the already completed runtime probe. The control-credential copy script's
hash was recorded after execution; its explicit provenance says so and must
not be read as a pre-execution attestation.

Earlier failed attempts remain private and are summarized in
`execution_failures_preserved`. Notable corrections were:

- SeaweedFS metadata occupied four initial volume slots. Increasing the owned
  deployment to eight slots preserved its PVC/data and the 128 MB per-volume
  limit. A successful metadata request alone was not accepted as object PUT.
- The imported OCI graph needed the exact descriptor-verified name alias.
  Packaging probes use `/app/.venv/bin/python`, not system Python. UID 10001,
  imports and libpq passed; full app startup did not run.
- Attachment transfer rejected an unknown extra object until its retained
  readiness-canary provenance was independently verified and explicitly
  allowed. Unknown extras and conflicting destination bytes still fail.
- Pooler testing first encountered an unsupported driver attribute, then a
  SCRAM verifier used incorrectly as a plaintext password. The final probe
  corrected both, rejects specific expected failures and reconnects normally.
  App IRSA was denied access to the control secret; its policy was unchanged.
  The existing operator read the exact secret in memory and verified both
  SCRAM keys before creating the limited target probe credential.

The source attachment adapter performed no writes. Destination creation used
conditional create and readback; neither bucket was cleared to obtain matching
counts. The extra readiness canary is reported separately from DB attachments.

## Reproduction and remaining integration

Use the [target workload guide](../../../infra/envs/target-workloads/README.md)
and the fixed source commits above. Verify the intended cluster namespace
identity, VM UUID/process start identity and current resource ownership before
using an environment. Fresh initial copy requires a verified empty target and
no preexisting subscription/origin/slot state. Do not reapply `copy_data=true`
to a populated database or assume CNPG reconciles runtime ENABLE/DISABLE.
Keep the 1 GB slot retention bound; it applies at checkpoints and is not a
whole-disk quota. An invalidated slot requires explicit resynchronization.

Public renderers produce manifests/SQL; they do not recreate private
credentials or automatically replay the live operator run. The private
operator handoff retains exact identity checks, timed receipts and restoration
procedures. Source receipts and execution hashes allow an authorized reviewer
to compare the retained run without publishing those inputs.

Repository scripts: 123 tests passed. Changed Python Ruff, compilation and
Terraform formatting passed. Publication privacy/secret scans and final-head
remote CI are recorded in the PR; none replaces team review or runtime checks.

Remaining integration is explicit:

- The fixed image still calls AWS Secrets Manager at startup. T9's reviewed
  OpenBao/ESO credential contract and T15's application validation/approval
  flow must precede actual destination business startup. Environment injection
  alone does not remove this SDK dependency.
- T12 still needs reviewed DB ownership/role delegation, barriers and probes,
  durable runtime identity checks and the platform Executor's actual network
  paths. The source and destination app credentials are independent; changing
  only a backend hostname is not an approved credential transition.
- Full cutover/failback, final demo hardware, realistic latency/size criteria,
  HA, backups and full T9 security controls remain unverified. T8/T30 stay open.

The development VM, retained volumes, source fixture and shared tools remain
available for the next owner. This evidence does not authorize their deletion
or a change to the organization's private-package policy.
