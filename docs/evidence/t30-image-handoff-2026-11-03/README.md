# T30 offline image handoff evidence

The original reviewed source/image was exported from private ECR at10:53:48Z,
using the existing operator identity and official ECR credential helper with
auth caching disabled. Export contains the original OCI index and all15blobs,
including amd64 and attestation manifests. No registry token or new credential
was handed to the VM.

`export-summary.json` derives from the original private export receipt and
records its hash. It clarifies that no new long-lived key was created while
temporary ECR authentication was used; the original generic credential field
was ambiguous. The working export tool was not yet committed and did not record
a pre-execution script hash; none is retrospectively assigned. `final-verification-receipt.json` records
an independent archive-only verification with its actual verifier source hash;
no AWS/API call is performed in verify-only mode (auth-cache status is therefore
null). The archive hash is the same in both observations. The final verification receipt is byte-identical to its private bounded
original; the export summary is explicitly derived as described above.

The actual81,745,920byte archive is operator-private and not committed. The
private VM contract belongs to the T30 builder; this PR creates no VM/cluster
resource or SSH session. Target import/Pod execution and full application startup
are not yet demonstrated by these receipts. The fixed app still unconditionally
reads AWS Secrets Manager; see `docs/operations/target-image-handoff.md` for the
T9/S2 gap and the separate configuration and data-routing requirements.

The source and digest remain exactly those already scanned/published/copied in
[the original private transfer](https://github.com/Softbank-Hackathon-Finals/demo-chat/actions/runs/41610561593).
No new vulnerability scan, changed source image, successful target application
read/write, or completed T30 is claimed by this export.


## Subsequent verifier correction and offline recheck

An independent local review reproduced a receipt-binding race: replacing the
archive path between graph verification and the separate checksum read could
record the replacement's checksum as verified. The correction in
`aedb4cf0548af6f4d7f9b3d50a5afb691a838de8` reads graph, checksum and size through
one open file and rejects path replacement or in-place metadata changes before
issuing a receipt. Regression tests include a same-size write with restored
mtime; it is still rejected through ctime comparison. The receiving operator
must compare the checksum again before import because later file changes are
outside the observation.

`post-fix-verification-receipt.json` is a new offline observation at
2026-11-03T11:31:10Z using that committed verifier. It is byte-identical to its
private bounded original and verifies the same 81,745,920-byte archive and
15-blob graph. The archive checksum remains
`04ad976a662c973920b8f3b4e39b80833bb9fda0f5ca15e097bf9aeb47fc8d24`.
The prior export and verification receipts remain unchanged; the new verifier
hash is not assigned to the earlier export. No image download, transfer,
registry authentication, target import or cloud mutation was repeated.
