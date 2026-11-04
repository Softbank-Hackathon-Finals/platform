# T7 scoped GitHub access verification

The live API probe passed at 2026-11-04 04:02 UTC.
Both credentials read the same `demo-chat` commit.
The writer created a new reference before and after the reader's permission denial.
The verifier confirmed that all temporary references were absent after cleanup.

| Evidence | Scope |
|---|---|
| [API verification](api-verification.json) | Actual GitHub API observations, fixed repository ID, exact commit, controls and cleanup |
| [Provisioning metadata](provisioning-metadata.json) | Provider settings readback and local protected-store readback; no credential values |
| [Source manifest](source-manifest.json) | Exact verifier and regression-test bytes used for this probe |

The credentials select only `Softbank-Hackathon-Finals/demo-chat` and expire on
2026-11-11. The reader has Contents-read and Metadata-read. The writer has
Contents-write and Metadata-read. Neither has organization permissions.
The issuer stored both credentials in macOS Keychain and verified their readback.

Twelve local regression tests cover positive controls, permission denial,
unknown responses, interrupted body reads, excess reader write access, cleanup and exit codes.
The live probe did not change files, existing branches, default-branch rules or deployments.

This proves the issued credentials' bounded repository capabilities.
It does not prove T31 approval enforcement, approved patch application or deployment.
The T31 owner must agree the credential input and protected delivery destination.
Consumer delivery remains open and is recorded as false in the metadata.
The T7 image delivery checkbox is a separate work item.

See the [access runbook](../../operations/github-repository-access.md) for permission
limits, replacement, handoff and the probe's reference-delete race boundary.
