# Resource ownership and admitted Pod checks

The first receiver could adopt a replacement between a successful create and
the first GET. After a lost create response it could delete an object with the
same name and label without knowing whether it created that object. A second
counterexample changed the container command to print the expected UID. The
original check accepted that output even when the admitted security context or
mounted credentials differed from the request.

The [source manifest](source-manifest.json) identifies the corrected receiver
and tests. The receiver compares its create-response UID to each object it
uses or deletes. An unacknowledged create leaves a null UID for the operator
to reconcile and blocks automatic deletion. Each Pod observation checks the
requested execution and security settings before using status or logs.

| Local counterexample | Observed result on the corrected receiver |
|---|---|
| Same-name, same-label Secret replaced before first GET | Replacement preserved, zero DELETE requests |
| Create response lost and object present | Object preserved, cleanup unverified |
| Expected UID printed by a substituted command | Pod rejected before accepting the UID |
| Root execution or injected projected token | Pod rejected |
| Late command change or ephemeral container on a later poll | Pod rejected before using status or logs |

Target tests passed (20). The repository script suite ran 480 tests with seven
environment-dependent skips and no failures. A second session reproduced six
ownership and admission cases independently against the same receiver digest.
All fault injections used local fixtures.

## Development VM rerun

On November 4 at 14:13 KST the operator ran the receiver in the source
manifest against the same development VM and immutable image. Both
authenticated Pods produced a kubelet pull event, the expected image digest
and UID 10001. The anonymous Pod received an explicit authentication rejection
between those controls. The receiver confirmed the credential refresh,
preserved the existing Pod UIDs, and removed the five objects whose UIDs it
had recorded from create acknowledgements. It exited with code 0.

At 14:17:46 KST an independent read confirmed the cluster and namespace UIDs
and found zero remaining resources by the five names and by the run label.
The [public receipt](live-verification.json) keeps the observed digest,
control results, counts, timestamps and hash of the restricted original. It
omits credentials and operational identifiers. The VM run exercised normal
admission; the replacement and injected-Pod races were reproduced locally.
