# T7 repository access provisioning

The current demo uses a selected-repository, expiring **reader** in a trusted
source acquisition process. T31 promotes the image digest already checked by T15;
its apply path does not commit a branch, rebuild an image or accept a Git writer.
See [ADR-0021 §6](../adr/0021-approved-change-promotion.md),
[T7/T31 tasks](../tasks.md) and the [implemented apply path](../../services/apply-path/src/bravo_apply/path.py).
This access contract does not complete T31 policy application or gate integration.

## Current consumer and permissions

| Identity | Selected repository | Repository permissions | Consumer |
|---|---|---|---|
| Analysis reader | `Softbank-Hackathon-Finals/demo-chat` only | Contents: read; Metadata: read | Trusted source acquisition supplies a fixed revision to T11. Never give the credential to the model or generated code. |
| Previously issued writer | Same selected repository | Historical Contents: write grant | No current T31 consumer. Do not inject it or repeat its temporary-branch probe for T7 completion. |

The reader was issued on 2026-11-04 with expiry 2026-11-11. Do not add Actions,
Administration, Workflows, organization permissions or a classic `repo` scope.
The [original issuance and API receipt](../evidence/t7-github-access-2026-11-04/README.md)
records the selected repository and reader write denial. It remains historical
issuer-side evidence, not a recipient receipt. An organization approval request
or a public repository HTTP 200 alone is not usable authenticated access proof.

The unused writer remains a historical protected-store entry; this change does
not claim it has been revoked. It must not be distributed. Revoke unused entries
or replace the reader through the protected process before expiry; the acceptance
probe refuses the old credential from 2026-11-11 00:00 UTC. A future GitOps
consumer needs its own approved scope, delivery contract and recipient evidence.

## Protected delivery and recipient acceptance

Use the existing protected credential store and operator identity. Do not print
values, place them in plaintext files, Git URLs, environment variables or command
arguments, replace `gh` authentication, or reuse `PROJECT_TOKEN`.

The 2026-11-05 acceptance uses a dedicated source-reader Job in the platform RKE2
cluster, coordinated with the integration operator. It is separate from T31's
runtime and the integration operator's network/target-RBAC changes. The reader is
mounted only in that Job at `/run/bravo/github/token`; it is not a permanent
worker deployment or an end-to-end T11 run.

The trusted operator transfers the protected-store reader over an encrypted,
echo-disabled SSM interactive stdin channel with no session logging inputs.
The receiver confirms the cluster identity, Secret encryption and a policy that
does not audit Secret request bodies before requesting the value. It creates the
Secret using API stdin, never a credential-bearing manifest file. Only a single
non-root container mounts the immutable Secret read-only. Service-account token
mounting is disabled; the Job has no Kubernetes RBAC and no extra or init
containers. A dedicated NetworkPolicy allows DNS and public HTTPS and denies
private/metadata destinations. The writer never crosses SSM or enters the recipient Job. The existing issuer
bridge reads its protected entries in local memory; the forwarding adapter keeps
only the reader and discards the historical writer without using it.

[`scripts/github_reader_probe.py`](../../scripts/github_reader_probe.py) is the
bounded recipient executable. For this issuance it pins repository ID
`1443045289`, authenticated actor ID `191002565`, revision
`dc9bece24898b752a77af1d1bbd440c11100e5f2` and `README.md`:

1. An unauthenticated `/user` request must fail with 401. The **same injected
   reader** must authenticate `/user`, return the expected repository identity,
   and read the exact commit.
2. Read the source file at that revision, decode its bytes, and compare its Git
   blob SHA and length. Record the source SHA-256, not the source contents.
3. Bind the receipt to the Job's actual Pod UID, unique run reference, time and
   executable hash. Verify the admitted pod has only the intended reader mount.
4. Delete only the owned namespace using a UID precondition, then query its
   absence independently. No leftover owned resources are allowed for PASS.

The probe makes GET requests only. It does not create Git refs, alter source,
build images, deploy the application or use T31's approval path. Errors yield
`UNKNOWN` and a fixed category without provider response bodies or credentials.
A transport timeout is not an invitation to rerun: reconcile the original run,
its credential-delivery state and owned resources first. Reuse valid existing
receipts and execute only a missing or failed operation after resolving its cause.

## Completion evidence

T7 item 5 requires the issuance/scope/expiry receipt, protected delivery, and a
recipient receipt satisfying the identity/repository/revision checks above.
An issuer-local test, dry-run or delivery notice alone does not satisfy it.
[The 2026-11-05 recipient packet](../evidence/t7-source-reader-2026-11-05/README.md)
contains the outcome and limits. Public receipts redact operational identities;
private original hashes preserve their relationship to the actual run.

After the contract and evidence PR merges, use `scripts/sync_tasks.py tick T7 5`
only if the item is still unchecked. Read issue #12 and Projects independently
and record 5/5, Closed and Done. T31's policy and gate requirements remain open
until its owner verifies them; this access packet does not check T31 items.

References: [fine-grained token management](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens),
[Git reference API and Contents permission](https://docs.github.com/en/rest/git/refs).
