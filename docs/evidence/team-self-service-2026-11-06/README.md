# Team self-service diagnostics — 2026-11-06

Actual RKE2/ARM64 execution through the existing personal-login direct API wrapper. No VM administrator kubeconfig or SSH was used for these trials. The environment ConfigMap was create-only installed and its UID recorded privately. Personal mappings, endpoints, raw receipts and original report files remain private.

[Summary](summary.json) distinguishes test verdicts, command status and this run's resource cleanup. T3 passed its required checks. The credential fixture passed twice (the final one through the ordinary CLI); two preceding UNCONFIRMED reports remain preserved, with cleanup complete. Their original probe failure was not retained, so their precise cause is not asserted. Added safe Pod status and probe observation recording makes later failures inspectable without retaining credential values.

A controlled SIGKILL left two namespace UID receipts. A separate command obtained fresh personal credentials, read the same run, deleted those UIDs and confirmed absence. The interrupted trial remains UNCONFIRMED; cleanup is CLEAN. Another preexisting rehearsal namespace was preserved and was never counted as this run's cleanup obligation.

Local regression coverage includes replaced UID, foreign run marker, lost create response, duplicate create intent, Secret body exclusion, read failure, same-run cleanup, missing central profile recovery, different member refusal and frozen renderer modification. T15's existing external cleanup script also ran successfully on the two inconclusive attempts before their control objects were deleted.

**Scope correction (#466 review).** The executing commit SHA was not recorded for these trials, so this evidence is not bound to a source revision. The PR description also mentioned a "SIGKILL right after the writer → external role/value deletion" trial and a "space-in-path ZIP T3" trial; neither is recorded here, so neither is claimed. The interruption above left only two namespace UIDs and does not exercise the recovery-Job path; that path is covered by local regression tests only. Later runs must record the executing SHA per trial.

This evidence does not establish another member's device login, WSL acceptance, production AI proposal execution or cutover completion. Runtime token provisioning and product host/GitOps remain owned by their existing implementations. Usage and boundary map: [team self-service guide](../../team-self-service.md).
