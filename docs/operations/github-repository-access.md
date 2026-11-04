# T7 repository access provisioning

The demo uses separate, expiring fine-grained personal access tokens for source
analysis and trusted branch writing. This implements the credential option in
[design appendix A](../design.html) and [T7](../tasks.md).
It does not implement T31 approval, patch selection, image publication or deployment.

## Provisioning contract

| Identity | Selected repository | Repository permissions | Consumer |
|---|---|---|---|
| Analysis reader | `Softbank-Hackathon-Finals/demo-chat` only | Contents: read; Metadata: read | Trusted source reader supplies a fixed revision to T11. Do not give credentials to the model. |
| Approved-change writer | `Softbank-Hackathon-Finals/demo-chat` only | Contents: read and write; Metadata: read | Trusted T31 process after its own approval checks. Never supply this token to T11, generated code or a rehearsal container. |

Use the organization as the resource owner, selected-repository access and a
seven-day expiry for this demo. Do not add Actions, Administration, Workflows,
organization permissions or repository-wide classic `repo` scope. Record the
actual expiry and permission selections when issuing the tokens. A permission
request awaiting organization approval is not usable access.

Fine-grained tokens restrict repositories and API permissions, **not branch
names**. The writer can modify unprotected references throughout its selected
repository. T31 must enforce approved diff/revision and its permitted branch
names before using the credential. Current default-branch rules remain necessary;
this verifier does not test or bypass them. Public content is readable without
authentication, so a successful read alone does not prove token scope.

Store credentials in the existing protected credential store. Do not print them,
save them in plaintext files, insert them in Git URLs or pass them as command-line
arguments. Do not replace the developer's `gh` authentication or reuse
`PROJECT_TOKEN`. Deliver only to the agreed consumer through its existing secret
injection mechanism. Consumer delivery and revocation remain explicit handoff steps.

## Bounded verification

The verifier reads a credential-store-produced JSON object from **stdin**, with
exactly the `reader` and `writer` keys. Supply a new receipt path on every run:

```sh
# Pipe the protected store's output directly; never write it to an input file.
python3 scripts/verify_github_access.py \
  --repository Softbank-Hackathon-Finals/demo-chat \
  --repository-id 1443045289 \
  --write-probe --receipt /private/verification/new-github-access-receipt.json
```

The explicit write probe creates at most three unpredictable
`t7-access-probe/<run>-<role>` branches, all pointing at the observed default-branch
commit. It changes no file, existing branch, default branch or protection setting.
Creating/deleting probe references can produce normal GitHub events.

Both credentials must read the expected repository identity and exact commit.
A successful writer reference creation precedes and follows the reader's denial
attempt. The reader must receive GitHub's specific personal-token permission
denial, with non-exhausted rate limits and required Contents-write permission.
Generic 403, 401, 404, timeout and server failure are `UNKNOWN`, not denial proof.
A reader that can create the probe branch yields `FAIL`.

The verifier attempts cleanup even when a create response is lost. It reads each
owned random reference, deletes only a reference still at the expected commit,
then verifies absence independently. It preserves an observed advanced reference
and reports `UNKNOWN`; inspect the named reference with its owner. GitHub's
reference-delete API has no compare-and-swap parameter, so this is a bounded
operator probe, not a concurrency-safe production branch deletion service.
No other process should write to these unique probe names during verification.

Exit codes are `0` for `PASS`, `1` for excess reader write permission, and `2` for
`UNKNOWN`. Cleanup failure cannot produce `PASS`. Existing receipts are never
overwritten. Receipts contain only public repository/revision and probe metadata;
provider error bodies and credential values are excluded.

## Completion and consumer handoff

T7 access completion requires issued permission/expiry metadata, this actual API
verification, protected storage, and the intended consumer's credential handoff.
Local regression tests alone do not complete the issue checkbox. The verifier
always records `t31_consumer_verified: false`: its temporary-reference writes do
not prove T31 accepted an approval or applied the approved diff.

T31's owner keeps approval and source/branch checks in its existing implementation.
Coordinate the credential input name and delivery destination before modifying
that implementation. Track expiry, replacement and revocation with the receiving
owner. Never send token values through issue comments, chat or review evidence.

References: [fine-grained token management](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens),
[Git reference API and Contents permission](https://docs.github.com/en/rest/git/refs).
