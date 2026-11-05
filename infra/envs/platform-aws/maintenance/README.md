# T7 SSM inotify capacity

Use this host maintenance step when SSM cannot start session/document workers because
the root user's inotify instance quota is exhausted. It requires the existing authorized
operator and the identified T7 host. It does not restart SSM, RKE2, workloads or the host.

## Diagnose before changing the quota

- Read the current SSM instance identity, active sessions, command status, agent version
  and executable paths. Preserve the original failure receipts. An Online heartbeat does
  not prove that a new worker can start.
- Check disk bytes/inodes, available memory, process FD limits and
  `/proc/sys/fs/inotify/max_user_instances`. Count inotify descriptors by UID using
  `/proc/*/fd` and `/proc/*/fdinfo`. The per-user instance limit is distinct from the
  number of watched files and each process's general FD limit.
- A bounded `inotify_init1` allocation probe must close every descriptor it opens.
  `EMFILE` with few process FDs and a saturated user quota supports instance exhaustion.
  Preserve this result separately from a count, which can change during concurrent work.
- Reconcile stale sessions with their owners. Match complete SessionId, target and
  document against the provider before terminating an owned session. Names can contain
  periods; extracting only an alphanumeric prefix is unsafe. Do not kill all sessions
  belonging to the same AWS login or restart the shared agent to hide a cleanup failure.

The 2026-11-05 incident had 127 root-owned inotify descriptors against a ceiling of 128,
while disk, memory and ordinary FD limits had headroom. Both new Port sessions and
RunCommand workers failed intermittently. A bounded probe later opened only five
instances before `EMFILE` and closed all five. A separate inventory caller's truncated
SessionId parser was identified by its owner; that caller's cleanup fix remains its
owner's change. Raising the quota does not replace that fix.

## Apply once and verify

1. Confirm the intended EC2 instance and Kubernetes cluster UID with the current owner.
   Record the old quota and active sessions. Leave other owners' sessions running.
2. Test a runtime-only increase: `sudo sysctl -w fs.inotify.max_user_instances=1024`.
   Use a compare-before-write check against the observed old value. If a RunCommand
   result is missing or malformed, read the value before deciding whether to retry.
3. Verify bounded inotify allocation, a fresh RunCommand, and a new authenticated
   operator Port session. Validate the forwarded service's TLS name/CA. Close the
   probe session, verify its provider terminal state and local port absence, and
   confirm the agent PID and Kubernetes health remain stable.
4. After the runtime check, install this reviewed file with mode 0644 as
   `/etc/sysctl.d/60-bravo-ssm-inotify.conf`. If the destination already exists,
   require identical bytes; stop on a different file. Do not overwrite another
   owner's setting or run a global `sysctl --system` during shared work.
5. Read back both the exact file and runtime value. Applying the same file again must
   make no change. This file persists the next boot's setting; no reboot is required
   to retain the tested live value. Inspect other sysctl files for a conflicting later
   override before reporting persistence.

1024 gives bounded headroom above the observed 128-instance ceiling for the existing
RKE2 processes and transient SSM workers. It raises a limit rather than preallocating
1024 watches. Keep `max_user_watches`, queued-event and general FD limits unchanged.
If usage continues to rise, fix the session/process lifecycle rather than repeatedly
raising the ceiling. Rollback is an owner-coordinated change: first reduce live usage
below the previous ceiling, then restore the recorded file/runtime value.

This is an existing-host maintenance configuration. It deliberately leaves the shared
T2/T7 cloud-init and Terraform user data unchanged; changing those inputs can replace
the retained EC2 host. A new host needs this reviewed maintenance step after bootstrap.

References: [AWS Session Manager troubleshooting](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-troubleshooting.html),
[AWS inotify and plugin troubleshooting](https://repost.aws/knowledge-center/ec2-session-manager-plugin-not-found).
