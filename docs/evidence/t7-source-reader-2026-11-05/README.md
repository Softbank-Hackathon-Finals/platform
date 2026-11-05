# T7 source-reader recipient acceptance — 2026-11-05

**PASS at 16:31 KST.** A dedicated Job in the platform RKE2 cluster received the
issued reader through the protected injection path and authenticated to
`Softbank-Hackathon-Finals/demo-chat`. [Recipient result](recipient.json) and
[source manifest](source-manifest.json) bind the result to the executable bytes.
This is recipient runtime evidence, not the earlier issuer-local probe.

| Check | Observed result |
|---|---|
| Authentication | Anonymous `/user` 401; same injected reader `/user` 200 with expected actor ID |
| Repository | `Softbank-Hackathon-Finals/demo-chat`, ID `1443045289` |
| Revision | `dc9bece24898b752a77af1d1bbd440c11100e5f2` |
| Source | `README.md`, 9,348 bytes; blob `d26bf6d1d8ea6c21b00e29f77b0d8da61324e1b1` and SHA-256 recorded |
| Execution | Unique run `cffe034eec784748bd44de1ff853db4a`; admitted single reader container; Pod Succeeded |
| Git writes | 0; no writer credential entered the receiving Job |
| Cleanup | Owned namespace deleted with UID precondition; independent GET confirmed absence at 16:31:30 KST |

The source-reader execution scope was coordinated with the existing integration
operator. Its isolated namespace, Secret and Job did not modify T31 code, the
integration operator's Tailscale/target-RBAC resources, or the cluster's shared
network configuration. This proves protected source-reader access; it does not
claim a permanent worker deployment, T11 end-to-end execution, T31 application,
or T31 policy/gate completion.

The [runbook](../../operations/github-repository-access.md) records the delivery
boundary. The existing trusted Keychain bridge supplied its entries through an
anonymous local pipe; the forwarding adapter retained only the reader, discarded
the historical writer without using it, and sent the reader over echo-disabled
SSM stdin. The receiving API encrypted the immutable Secret, its audit policy did
not log request bodies, and only the non-root reader container mounted it.
Credential values were not saved in evidence, messages, argv or logs.

Two earlier attempts did not deliver a credential or contact GitHub: the first
missed the nested PTY readiness line and timed out; after line normalization, the
second waited on a separate Keychain CLI authorization prompt. That prompt was
cancelled and namespace absence was independently reconciled. The successful
attempt reused the existing trusted store bridge. Neither failed attempt is
counted as acceptance. No issuer API/write probe, item 4 verification or host
reconfiguration was repeated.

Operational UIDs are hashed and private resource names/registry identity omitted
in the public projection. [Projection manifest](projection-manifest.json) binds
it to the protected original bytes. An authorized reviewer needs the private
original to identify the actual resources. The probe was uncommitted at execution;
its subsequently created source commit contains byte-identical code, verified
against the recipient-emitted hash. The source manifest states that chronology.
Local identity/repository/revision/source-corruption and expiry tests: **7 passed**.

## Official scope and prior completion evidence

[PR #265](https://github.com/Softbank-Hackathon-Finals/platform/pull/265) merged
T31's verified-digest promotion path. Its function has no Git writer input and
performs no branch commit or image rebuild. The
[owner's digest/diff statement](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793858043899199)
and implementation support the current path. Under the user's direct instruction
to resolve the stale requirement, this PR aligns T7/T31 task text and design
v0.6. It does not represent an unanswered writer-retirement question as explicit
owner agreement. [ADR-0021 §6](../../adr/0021-approved-change-promotion.md) records
that distinction. T31's policy and gate requirements remain unchanged.

The following existing evidence was read again, without rerunning it:

- [PR #184](https://github.com/Softbank-Hackathon-Finals/platform/pull/184) is merged;
  its [issuance/scope/expiry and issuer API receipt](../t7-github-access-2026-11-04/README.md)
  remains valid historical evidence. The unused writer is not distributed or
  claimed revoked. The reader acceptance refuses the old credential from
  2026-11-11 00:00 UTC.
- [OIDC run 41602534226](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41602534226)
  remains successful; the [bounded deployment receipt](../t7-runtime-verification-2026-11-03/actions-oidc-readback.json)
  shows the fixed-namespace deployment and negative access checks.
- [GHCR run 41606831578](https://github.com/Softbank-Hackathon-Finals/demo-chat/actions/runs/41606831578)
  remains successful. Its retained artifact reports digest
  `sha256:001adc35a9fc767566552fb55c3c19a67b7c5efc7231fb02dc28cdeace99d0d4`;
  [artifact readback](ghcr-publication.json) preserves its publication and registry
  pull results without relabeling them as cluster pull evidence.

[Provider readback](baseline-readback.json) records current merged/run states.
After this PR merges and the revised issue text is synchronized, T7 item 5 may
be checked using the team tool. Issue 5/5/Closed and Projects 5/5/Done still need
separate post-write readback; this packet alone does not assert those states.
