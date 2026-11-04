# T7 target development registry access

This procedure checks private image access on the separate local development
VM used by T30. It does not select the final demonstration hardware, deploy the
T31 application, or change the destination registry architecture.

Private GHCR remains the publication source. The existing
[private delivery decision](../adr/0011-private-image-delivery.md) mirrors its
OCI digest into ECR without rebuilding. This procedure checks that mirror from
the development target. It does not make ECR a permanent on-premises dependency.

## Credential boundary

`scripts/target_registry_credential.py` issues a one-hour STS federation session
from the operator's existing IAM user. No IAM user, role, access key or policy
is created or changed. The session policy allows registry authorization and
three pull actions on only the `bravo/demo-chat` repository. Explicit denials
exclude other actions and other repositories, including additional grants from
resource policies.

The temporary AWS credentials stay in the operator process. Only the ECR
authorization is passed through protected stdin to the verified target SSH
connection. Neither the input payload nor credentials are written to files,
arguments, logs or evidence. The module deliberately has no credential-printing
CLI. The operator uses the existing protected AWS credential store; this
procedure does not distribute that identity to the target.

Record the returned STS and registry expiry times separately. ECR documents a
12-hour authorization lifetime, while this STS session requests one hour. The
operator uses the earlier returned time as a conservative refresh deadline.
An immediate refresh test does not prove expiry followed by renewal. There is
no background refresh service in this change.

## Target identity and audit preflight

Before issuing credentials, reconcile the existing VM process and VM UUID,
SSH host key, `kube-system` namespace UID, target namespace UID and Node UID with
the owner-confirmed private access contract. Persist a random 32-hex `probe_id`
and those bindings in a restricted intent receipt before any remote effects.

`scripts/target_registry_probe.py` independently checks the cluster, namespace
and node UIDs before creating anything. It refuses an existing RKE2 registry
configuration. The supported development VM currently uses the exact audit
policy `None`: the probe checks its bytes, running API server argument, and
modification time relative to API server start. This avoids recording Secret
request bodies but does **not** meet a Metadata audit requirement. Do not reuse
a separate VM's Metadata-audit evidence for this VM or silently change its
audit policy. A different policy requires separate review before this probe.

## Protected operator sequence

1. Freeze the issuer and receiver source commit, the published OCI index and
   platform manifest digests, and the private target binding. Save their
   identifiers and source hashes without credentials.
2. Call `target_registry_credential.issue(contract)` twice in the operator
   process. The contract contains `account_id`, `region`, `repository_arn` and
   `repository_url`; the code validates their exact relationship. Preserve only
   expiry metadata in evidence.
3. Pass JSON through SSH stdin to the target-local receiver running as the
   existing authorized administrator. Required input fields are `probe_id`,
   `namespace`, `repository_url`, `digest`, `platform_digest`, `cluster_uid`,
   `namespace_uid`, `node_uid`, and two `authorizations` containing `registry`,
   `authorization` and `refresh_before`. Do not use shell tracing or save this
   JSON. Execute only the reviewed receiver source, with stderr kept private.
4. Check exit code `0`, `status: PASS`, every control, and
   `cleanup_verified: true`. Exit code `2` and missing or failed observations
   are `UNKNOWN`. A lost SSH response is also UNKNOWN; reconcile resources
   using the saved probe ID before retrying.

The receiver creates a new namespaced pull Secret and a NetworkPolicy selecting
only its random probe label. It runs authenticated pull, anonymous pull, Secret
refresh, and authenticated pull again against the same immutable image. Each
probe Pod uses `imagePullPolicy: Always`, UID 10001, no service-account token,
a read-only root filesystem and a command that prints only its UID. It makes
no application or database requests.

Both authenticated controls require the expected runtime digest, UID and a
successful image-pull event bound to that Pod UID. Before using container
status or logs, the receiver compares the observed Pod with the requested
command, arguments, environment, security settings, pull credentials and
owner label. Extra containers, volumes (including projected tokens), mounts
and devices invalidate the observation. Scheduling defaults and standard
termination-message defaults are accepted. This check assesses an admitted
Pod; cluster admission policy controls what may start running.
Anonymous access counts as
denied only for an explicit authentication rejection. Timeout, DNS failure,
missing controls, unexpected ambient credentials and generic image errors are
UNKNOWN. The test does not delete the container image cache; the anonymous
control must still be denied when layers already exist locally.

## Cleanup and handoff

Secret refresh uses UID and resource-version checks. The receiver records each
UID from the successful create response before reading the object by name.
It checks the current UID and owner label before sending a deletion with the
recorded UID as an API precondition. A replacement is preserved, including a
replacement carrying the same owner label. The receiver verifies removal and
checks the pre-existing application Pod UIDs.

If a create response is lost, the receipt records the uncertain resource name
with a null UID. Automatic deletion stops for that resource and the result is
UNKNOWN. For independent recovery, check the saved intent and obtain the
original create UID from a trusted acknowledgement. Compare it with the current
UID and label and use that UID as a deletion precondition. If no such evidence
exists, leave the object in place for the namespace owner and record the
unresolved cleanup. Keep recovery evidence separate from the failed attempt.

The probe Secret is removed, so this is a bounded access and manual refresh
test, not a standing deployment credential. Before T31 uses the path, its owner
must agree the protected injection location, Secret reference and refresh
owner. T7 does not require a completed T31 application or cutover to hand over
access. T7 issue checkboxes remain subject to merged review and their actual
completion criteria.

Sources: [STS federation sessions](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetFederationToken.html),
[ECR registry authentication](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html),
[kubectl raw deletion and stdin](https://github.com/kubernetes/kubectl/blob/v0.34.1/pkg/cmd/delete/delete.go).
