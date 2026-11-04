# T7 target development registry verification

The owner-confirmed local development target pulled the existing private ECR
mirror of the GHCR image with the same immutable digest. The receiver returned
`PASS` and exit code `0`; a later independent read found zero probe resources.

| Observation | Result |
|---|---|
| Existing VM, cluster, namespace and Node identity | Matched the private owner contract |
| First authenticated `Always` image pull | Expected digest, actual pull event and UID 10001 execution |
| Anonymous control against the same image | Explicit registry authentication rejection |
| Credential refresh | Same Secret UID, resource-version update and data readback verified |
| Second authenticated `Always` image pull | Expected digest, actual pull event and UID 10001 execution |
| Existing application Pods | UIDs preserved |
| New probe resources | Deletion preconditions used the UIDs from the first readback of one Secret, one NetworkPolicy and three Pods |
| Independent cleanup readback | Zero remaining, 2026-11-04 04:37:26 UTC |

Both positive controls ran
`sha256:001adc35a9fc767566552fb55c3c19a67b7c5efc7231fb02dc28cdeace99d0d4`.
The probe did not rebuild an image, change an existing application or contact
the application database. It did not delete cached image layers.

The temporary AWS sessions stayed on the operator Mac. ECR authorizations were
passed only through protected stdin and a new target pull Secret. Evidence
contains separate session/registry expiry metadata, never credential values.
The probe Secret was deleted; no standing consumer credential was installed.

## Evidence and limits

- [Live verification](live-verification.json) is an explicitly derived public
  summary. It retains observations, times, digest, booleans and exit code while
  omitting private account/registry and operational resource identifiers. Its
  private receipt hash binds the original, which is retained with restricted
  access. This is not a raw provider receipt or an independently public identity
  verification.
- [Source manifest](source-manifest.json) pins the exact committed issuer,
  receiver and tests at execution. The receiver bytes were independently
  compared after the run. The subsequent issuer refactor only builds ARN
  components in one function, preserving the generated strings; the manifest
  continues to name the actual earlier execution source.
- The original receiver obtained cleanup UIDs from a later GET and checked only
  part of the admitted Pod spec. Its receipt cannot exclude a replacement
  before that GET or a changed command that printed the expected UID.
  The [follow-up verification](uid-admission/README.md) records the correction
  separately. The original receipt and source manifest retain their bytes.
- Target-specific local regression: 16 tests passed. The repository script
  suite at the execution source passed 476 tests with seven environment-dependent
  skips. CI on the submitted PR is a separate result.

The current development VM's bound audit policy is `None`. This protects Secret
request bodies from audit logging but is **not** Metadata audit verification.
The separate Metadata-audit test VM's evidence is not applied here.

Immediate credential refresh does not establish automatic renewal or behavior
after expiry. This is a local development target, not final physical demo
hardware. No T31 approved diff, product deployment or cutover was executed.
Consumer injection and refresh ownership must be agreed before leaving a
standing credential. T7 checklist completion also requires merged review and
the task's existing completion criteria.

Use the [operator procedure](../../operations/target-registry-access.md) for
identity checks, protected transport, failure reconciliation and handoff.
