# T7 private registry evidence

These receipts cover distinct observations; an earlier false/pending flag is
retained as history, not rewritten into a later success.

- `registry-readback.json`: initial registry/IAM creation, before image copy.
- `policy-verification.json`: exact-repository IAM simulation, policy validation
  and Terraform drift check. This is not a human login test.
- `private-transfer-receipt.json`: successful main-branch Actions run, exact
  original index and child manifests, all 15 OCI blobs byte equal, actual private
  registry pull and runtime probe. This is the application-image scan evidence.
- `credential-provider-source.json`, `provider-install-readback.json`,
  `provider-idempotent-readback.json`, `kubelet-pull-readback.json`: original
  official EKS provider installation and successful current image pulls. These
  do **not** prove that the original provider was free of fixable vulnerabilities.
- `provider-fixed-build-verification.json`: the original coverage-zero scan
  correction, original rootfs negative control, and independently reproduced
  dependency-fixed binary with positive Go-binary/package coverage. Build inputs
  and the exact dependency patch are in `infra/envs/private-registry/`.
- `provider-fixed-build-readback.json`, `provider-fixed-install-readback.json`,
  `provider-fixed-pull-readback.json`, `provider-fixed-idempotent-readback.json`:
  actual host reproduction of the same binary, its atomic replacement, healthy
  node/system readback, two new successful private pulls, and a final invocation
  without replacement or restart. Each binds the observation to the executed
  script hash. These supersede the original provider for current installation
  and pull state; they still do not claim expiry-and-renewal observation.

The original single-file filesystem scan recognized no packages and cannot be
used as a clean scan result. A proper rootfs scan recognized 53 packages and
four fixable HIGH findings in the original official provider. The fixed binary
recognized 54 packages and zero fixable HIGH/CRITICAL findings. Raw scans and
command/resource mappings remain operator-private; public receipts contain only
bounded metadata. No scan severity/ignore exception was introduced.

Successful pulls and observed provider cache metadata do not demonstrate expiry
and renewal. Neither the six-hour cache expiry nor twelve-hour registry-token
expiry was awaited. No long-lived registry credential or Kubernetes pull secret
was created, and these results do not complete all of T7 or prove individual
human Kubernetes login.
