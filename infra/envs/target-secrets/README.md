# Target OpenBao and ESO preparation

This is the T30 consumer prerequisite for T9 secret delivery. It adds a
single-node OpenBao service with TLS, retained local storage, Kubernetes
ServiceAccount authentication and namespace-scoped ESO reconciliation. It does
not translate the demo application, change DB LOGIN/subscriptions, or execute a
cutover. The approved T15 diff and T31 image remain separate inputs.

## Fixed consumer contract

| Object in `target-dev` | Keys | Purpose |
| --- | --- | --- |
| `target-app-credentials` | `credentials.json` containing `username` and `password` | Pre-cutover source app identity |
| `target-app-objects` | `access_key`, `secret_key` | Existing isolated SeaweedFS client |
| `target-app-admin` | `token` | Destination application's admin API |
| `target-openbao-ca` ConfigMap | `ca.crt` | Public CA certificate for HTTPS verification |

The existing `target-app-db` Secret belongs to DB preparation and is not adopted.
ESO refreshes every 30 seconds. The application proposal reads projected JSON
at `/run/bravo/database/credentials.json` using its existing 300-second cache
and re-reads after authentication failure. This path is a T15 approval input;
creating a projected-file probe is not proof that the unchanged app can start.

`target-eso-reader` authenticates with audience `openbao`. Its OpenBao policy
reads exactly three KV v2 paths, with no default policy, secret writes or token
creation. Token TTL is 300 seconds and maximum TTL 900 seconds. OpenBao uses its
own rotating mounted ServiceAccount token only for Kubernetes TokenReview.
The main ESO controller reconciles `target-dev`; the standard upstream webhook
and certificate controller retain their separate infrastructure RBAC. Do not
claim every ESO component has namespace-only permissions.

## Rendering and deployment order

Use the exact chart archives and hashes in `render.py`, and the actual Kubernetes
version in Helm. For the development proof this was RKE2 1.35.9. The renderer
requires Python 3.10 or newer; the Helm YAML conversion below also needs PyYAML
6.0.3 in a dedicated local environment. The renderer itself uses only the
standard library and performs no cluster or cloud API calls.

1. Verify the intended cluster and node identity, the absence of the new
   namespaces/resources and the scanned image's complete OCI descriptors.
2. Import the private offline image. Containerd needs the canonical
   `localhost/bravo-openbao@sha256:...` alias as well as the tag. Verify that both
   resolve to the expected descriptor before creating the Pod.
3. Render `foundation` for the verified node's `kubernetes.io/hostname` label,
   and `eso-namespace` separately. Create both namespaces before their namespaced
   resources. The foundation describes two-way PV/PVC binding with
   `storageClassName: ""` and Retain semantics. It does not create the host
   directory: the operator must exclusively create `/var/lib/bravo-target-dev/openbao`
   with mode 0700, UID 100/GID 1000 before starting OpenBao. Never reuse or
   overwrite another session's data path. `target-dev` must already exist from T30.
4. Generate the CA/leaf key in memory. Store recovery material in the operator's
   OS credential store, and create the Kubernetes TLS Secret directly from
   memory/stdin. `ca.crt` is public; private keys and init responses are not files.
   Check the complete CA/leaf chain with the consumer's default strict TLS
   verification, including hostname and certificate extensions, before handoff.
5. Render `bao-values` with Helm `--skip-tests --kube-version <actual version>`.
   Pass its parsed JSON objects through `finalize-bao` before deployment. This
   validates the namespace, image, retained-volume selection and service
   selectors, and replaces the chart's default skip-verification readiness
   command with verified TLS. Do not apply the unprocessed Helm output.
   `bao-values` also sets the readiness probe to timeout 10 s, period 10 s and
   failure threshold 3 (chart 0.30.6 defaults: 3 s, 5 s, 2). The TLS-verified
   `bao status -format=json` took longer than 3 s on the target and Ready
   flapped. `finalize-bao` refuses a StatefulSet with any other timing, so a
   render without `bao-values` fails instead of shipping the defaults.
6. Render `eso-values`, establish its CRDs, then install its controllers. Create
   OpenBao (`network`) and ESO (`eso-network`) NetworkPolicies using both the
   actual API Service address and its endpoint address. The policy accommodates
   API DNAT without allowing all private networks. Verify actual traffic rather
   than treating render success as a connectivity result.
7. Initialize OpenBao once; immediately preserve the complete response in the
   credential store before parsing individual fields. Reuse that recovery
   material on restart. Never repeat init or recreate storage to mask an error.
8. Enable KV v2 with CAS required, configure Kubernetes auth and `policy()`.
   Read the exact source Secrets Manager `VersionId` together with `AWSCURRENT`,
   then write KV with CAS 0. A version/stage drift stops the operation. Neither
   source credentials nor AWS workload permissions are changed.
9. Apply `delivery`; require all ExternalSecrets Ready. Compare values only in
   memory. Verify a real consumer's projected file and HTTPS authorization,
   negative service-account/path/write/token-creation checks, refresh and
   restart persistence before handing off.

The following commands produce configuration only. Supply the verified hostname
label, API addresses, Kubernetes version and downloaded chart paths in
`TARGET_NODE`, `API_SERVICE_IP`, `API_ENDPOINT_IP`, `KUBE_VERSION`, `BAO_CHART`
and `ESO_CHART`. Set `RENDER_PYTHON` to Python 3.10+ with PyYAML 6.0.3, and
`OUTPUT_DIR` to a new, private output directory outside the repository. Run the
commands from the repository root. These output files contain configuration,
not credential values.

```sh
renderer=infra/envs/target-secrets/render.py
"$RENDER_PYTHON" - "$renderer" "$BAO_CHART" "$ESO_CHART" <<'PY'
import hashlib, importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("target_secrets", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for path, expected in zip(sys.argv[2:], (module.BAO_CHART_SHA256, module.ESO_CHART_SHA256)):
    if hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest() != expected:
        raise SystemExit("chart hash mismatch")
PY
"$RENDER_PYTHON" "$renderer" foundation --node-name "$TARGET_NODE" > "$OUTPUT_DIR/foundation.json"
"$RENDER_PYTHON" "$renderer" eso-namespace > "$OUTPUT_DIR/eso-namespace.json"
"$RENDER_PYTHON" "$renderer" network --api-ip "$API_SERVICE_IP" --api-endpoint-ip "$API_ENDPOINT_IP" > "$OUTPUT_DIR/bao-network.json"
"$RENDER_PYTHON" "$renderer" eso-network --api-ip "$API_SERVICE_IP" --api-endpoint-ip "$API_ENDPOINT_IP" > "$OUTPUT_DIR/eso-network.json"
"$RENDER_PYTHON" "$renderer" bao-values > "$OUTPUT_DIR/bao-values.json"
"$RENDER_PYTHON" "$renderer" eso-values > "$OUTPUT_DIR/eso-values.json"
```

Use a shell with `pipefail`, so a failed Helm render or YAML conversion cannot
be hidden by the final command. `finalize-bao` accepts a JSON array or a
Kubernetes `List` on stdin and emits a Kubernetes `List`.

```sh
set -o pipefail
helm template target-openbao "$BAO_CHART" --namespace bravo-secrets \
  --values "$OUTPUT_DIR/bao-values.json" --skip-tests --kube-version "$KUBE_VERSION" \
  | "$RENDER_PYTHON" -c 'import json,sys,yaml; json.dump([o for o in yaml.safe_load_all(sys.stdin) if o],sys.stdout)' \
  | "$RENDER_PYTHON" "$renderer" finalize-bao > "$OUTPUT_DIR/bao.json"
helm template external-secrets "$ESO_CHART" --namespace external-secrets \
  --values "$OUTPUT_DIR/eso-values.json" --include-crds --skip-tests --kube-version "$KUBE_VERSION" \
  > "$OUTPUT_DIR/eso.yaml"
"$RENDER_PYTHON" "$renderer" policy > "$OUTPUT_DIR/target-app-reader.hcl"
"$RENDER_PYTHON" "$renderer" delivery > "$OUTPUT_DIR/delivery.json"
```

Establish the ESO CRDs before applying the remaining objects in `eso.yaml`.
Inspect its standard webhook certificate Secret as well as the infrastructure
RBAC. The renderer supplies no TLS private key, initialization response or
source secret value: those steps still require the operator's in-memory
credential-store procedure. Configuration rendering alone cannot reproduce
the one-time value migration or prove its runtime outcome.

Use server-side dry runs, retain pre-apply source/manifest hashes and verify
resource UIDs around changes. Secret-bearing commands must use stdin, never
argv, shell tracing or captured stdout files. `bao write -format=json` may wrap
endpoint results in `data`; validate the response shape before interpreting it.

This development deployment uses a single retained node, one unseal share and
manual recovery from the operator's credential store. It is not HA, automated
unseal, off-host backup or a production availability claim. The initial leaf
certificate is valid for 30 days and CA for 90 days; renew them before expiry.
The OpenBao UI/injector/CSI/dev mode are disabled.

## Rehearsal subtree for T15 (design 8.4)

T15 applies an AI credential-migration candidate inside an isolated `rehearsal-<id>`
namespace (label `app.kubernetes.io/part-of: bravo-rehearsal`). The candidate image reads the
same `/run/bravo/database/credentials.json`, so the rehearsal needs synthetic credentials
delivered the same way but from a path the application identity can never read.

| Stage | Produces | Scope |
| --- | --- | --- |
| `rehearsal-policy --run-id <run>` | HCL for policy `rehearsal-app-reader-<run>`: read `secret/data/bravo/rehearsal/<run>/*` only | One policy per run. No other run's path, no `bravo/target/*`, no writes, no token creation |
| `rehearsal-auth-role --namespace rehearsal-<id> --run-id <run>` | Body for `auth/kubernetes/role/rehearsal-app-reader-<run>` | ServiceAccount `rehearsal-eso-reader` in **exactly that namespace** (explicit binding, so OpenBao needs no `namespaces get` RBAC); this run's policy only; audience `openbao`; TTL 300/900 s |
| `rehearsal-delivery --namespace rehearsal-<id> --run-id <run>` | ServiceAccount + SecretStore + ExternalSecret projecting `bravo/rehearsal/<run>/database` into Secret **`target-app-credentials`** (same name as `target-dev`) | One namespace per attempt; `deletionPolicy: Delete` so the Secret goes with the namespace |
| `rehearsal-init-container --run-id <run>` | An init container that logs in with the reader token and writes `credentials.json` itself | Works while ESO stays scoped (below) |
| `rehearsal-writer-policy` | HCL for policy `rehearsal-openbao-writer` (T15 prep Job) | See "Rehearsal writer" below |
| `rehearsal-writer-auth-role` | Body for `auth/kubernetes/role/rehearsal-openbao-writer` | SA `rehearsal-openbao-writer` in exactly `rehearsal-control`; audience `openbao`; writer policy only; TTL 300/900 s; no default policy |
| `rehearsal-writer-identity` (optional) | Namespace `rehearsal-control` (PSS restricted) + ServiceAccount `rehearsal-openbao-writer` (no automount) | Apply only if T15/T30 agree T9 owns them; see below |

`rehearsal-auth-role` emits scalar strings (`"bound_service_account_namespaces":
"rehearsal-<id>"`), not lists, because the writer's parameter constraints only match
strings. Send the JSON on stdin (`bao write auth/kubernetes/role/<name> - < role.json`) so the
boolean stays a boolean; `key=value` arguments send `"true"` and are refused.
`rehearsal-control` is rejected as a reader namespace even though it matches `rehearsal-*`.

The T15 runner's prep Job, with the writer identity below, creates the run's policy and role,
writes the synthetic values to `bravo/rehearsal/<run>/database` (key `credentials_json`) before
the attempt, and deletes the role, policy and KV metadata afterwards; this renderer never
carries a value. Run isolation to prove on the cluster: a token from run A's namespace reads
A's path, and is refused on run B's path and on every `bravo/target/*` path.

### Rehearsal writer (agreed with T15, 2026-11-06)

The T15 prep Job runs in the management namespace `rehearsal-control` as ServiceAccount
`rehearsal-openbao-writer` and logs in with a projected token of audience `openbao`. Its Pod
template **must** carry `app.kubernetes.io/component: rehearsal-openbao-writer`: the OpenBao
`network` policy admits 8200/TCP from `rehearsal-control` only for Pods with that label (namespace
*and* label in one peer). The existing ESO and `target-dev` probe peers are unchanged.

Policy `rehearsal-openbao-writer` grants only:

| Path | Capabilities | Constraint |
| --- | --- | --- |
| `secret/data/bravo/rehearsal/*`, `secret/metadata/bravo/rehearsal/*` | create, read, update, delete | Trailing `*` is a prefix glob: `bravo/rehearsal-x` and `bravo/target/*` do not match. No list, destroy or undelete |
| `sys/policies/acl/rehearsal-app-reader-*` | create, read, update, delete | Policy *body* is unconstrained (residual risk below) |
| `auth/kubernetes/role/rehearsal-app-reader-*` | create, update, delete | `required_parameters` = all seven role fields; `allowed_parameters`: SA `rehearsal-eso-reader`, namespace `rehearsal-*`, audience `openbao`, policy `rehearsal-app-reader-*`, TTL `300s`/`900s`, `token_no_default_policy` `true`; `denied_parameters`: namespace `rehearsal-control*`, and for namespace and policy any value containing `,`, `*` or a space |
| `auth/token/lookup-self` / `renew-self` / `revoke-self` | read / update / update | |

Why the denials: OpenBao checks the raw request string, but Kubernetes auth then splits a
comma string into a list, trims it and treats `*` as a glob at login, so `"rehearsal-a,target-dev"`,
`"rehearsal-*"` or `"rehearsal-control "` would otherwise pass a `rehearsal-*` check. Any other
parameter (deprecated `policies`, `token_period`, `token_bound_cidrs`, a namespace selector, …) is
refused because it is not allowed. JSON lists are refused too: OpenBao compares a list value
as a whole against the allowed strings. OpenBao 2.7.2 also applies `required_parameters` to reads
(`internal/vault/policy/acl.go`), so the role path grants no `read`; existence is proved by the
reader login instead. The writer's own policy and role names fall outside
`rehearsal-app-reader-*`, so it cannot widen or rebind itself.

**Writer residual risk.** An ACL cannot inspect a policy body. The writer can write
`rehearsal-app-reader-evil` granting `secret/data/bravo/target/*` (or anything else) and bind it,
within every constraint above, to `rehearsal-eso-reader` in some `rehearsal-<id>` namespace.
Whoever can then obtain a token for that ServiceAccount reads `bravo/target`. The local check
confirmed this end to end ([evidence](../../../docs/evidence/t9-openbao-writer-2026-11-06/README.md)).
The writer SA alone cannot use it (it cannot bind its own SA, `rehearsal-control`, or a glob),
so the exposure is "writer compromise **plus** the ability to obtain a `rehearsal-eso-reader`
token in a rehearsal namespace" (any identity that can create Pods or TokenRequests there).
Until the follow-up below lands:

- keep the writer token short-lived (300/900 s) and revoke it at the end of the prep Job;
- give the T15 runner's Kubernetes identity no `serviceaccounts/token` or Pod-create rights in
  rehearsal namespaces beyond what the attempt needs, and never run model-proposed manifests as
  `rehearsal-eso-reader`;
- audit `sys/policies/acl/rehearsal-app-reader-*` writes (OpenBao audit device) and compare each
  body with `rehearsal-policy --run-id <run>`.

**Follow-up (safer alternative, not implemented).** Remove the writer's `sys/policies/acl`
access. Either (a) the T9 operator pre-creates a fixed set of reader slots
(`rehearsal-app-reader-slot-1..N`, each reading `bravo/rehearsal/slot-<n>/*`) and the writer only
binds roles to an existing slot, or (b) one operator-owned templated policy reads
`secret/data/bravo/rehearsal/{{identity.entity.aliases.<kubernetes accessor>.metadata.service_account_namespace}}/*`,
keyed by namespace instead of run id. Both change the KV path contract with T15 and need its
agreement; (b) has not been tested here.

**Network for readers.** The rendered OpenBao `network` policy has no ingress peer for
`rehearsal-*` attempt namespaces, so the `rehearsal-init-container` path cannot reach OpenBao
until a reader peer is agreed and added. This change does not add one.

**Namespace and SA ownership.** The T15 owner coordinates creating `rehearsal-control` with
T30. `rehearsal-writer-identity` renders the namespace and SA for that conversation; do not
apply it if T30 creates them. It deliberately omits `app.kubernetes.io/part-of: bravo-rehearsal`
so rehearsal selectors and cleanup never treat it as an attempt namespace.

**Local check.** `testing/writer_acl_kind.py --kubeconfig <kind kubeconfig> --output <private
file>` runs only on a single-node `kind-*` context. It deploys an upstream OpenBao 2.7.2 dev
server as a `target-openbao` stand-in, applies the rendered NetworkPolicy, writer policy and
roles unchanged, logs in with real ServiceAccount tokens and records each allowed/denied case,
the escalation case and the network path from a labelled Job. It writes no token or value. It is
not a substitute for the same checks on the demo RKE2 target with the TLS image.

The init script fetches into a private temp file (`umask 077`), checks it is non-empty and
has the expected shape, then replaces `credentials.json` with `mv -f`, which also succeeds
over the 0400 file left by an earlier attempt on the same emptyDir. Token revocation runs from
an `EXIT` trap on every path. A failed fetch exits non-zero and leaves no temp file, so a
stale earlier file is never mistaken for a fresh success.

**ESO scope.** The ESO release here is scoped to `target-dev` (`scopedNamespace`,
`scopedRBAC`), so it does not reconcile ExternalSecrets in `rehearsal-*`. Two ways to serve
them, in order of preference for the demo:

1. Use `rehearsal-init-container` in the rehearsal Pod template (same path, same policy, no
   ESO involvement). This keeps the production ESO permissions unchanged.
2. Run a second ESO release with cluster-scoped RBAC limited by the `processClusterStore=false`
   defaults and reconcile only namespaces carrying the rehearsal label. This widens ESO's
   Secret-write permission to rehearsal namespaces and needs its own review.

Neither has been exercised on the demo cluster yet; the T15 isolation test (#24 item 6) is the
place to prove the chosen path.

## Modified OpenBao image

The official 2.7.2 image contained fixable HIGH
[CVE-2026-116587](https://github.com/advisories/GHSA-mgcc-cww5-fp2q).
The local image uses exact upstream commit
`9e0a0d3c3504a0a00b45e61355afac5e35c78379`, the small
[dependency patch](image/go-jose.patch) upgrading go-jose 4.1.4 to 4.1.6, and
Go 1.27.3. This is a downstream development build, not an official release.
Upstream MPL-2.0 licensing remains applicable; the base image retains its license.

[Build receipt](image/build-receipt.json) records the exact build flags, binary
hash, resulting image digest and actual container version probe. In particular,
set upstream `internal/version.fullVersion=2.7.2`; setting only a vendor metadata
suffix leaves upstream's development version and is insufficient.

Reproduce in a new source directory:

```sh
git clone https://github.com/openbao/openbao.git openbao-source
git -C openbao-source checkout 9e0a0d3c3504a0a00b45e61355afac5e35c78379
git -C openbao-source apply /path/to/image/go-jose.patch
```

Use the receipt's `build_flags` with `GOTOOLCHAIN=go1.27.3`, `CGO_ENABLED=0`,
`GOOS=linux`, `GOARCH=amd64`. First run the patched dependency's tests on the
host. Copy the resulting binary as `bao` next to [Dockerfile](image/Dockerfile),
build for linux/amd64 and export an OCI layout. Scan those exact bytes with
Trivy's `vuln,secret`, `HIGH,CRITICAL`, `--ignore-unfixed`, `--exit-code 1` gate.
Require actual OS and Go-binary coverage. A scan execution error is not a pass.
Run the resulting container with no network and check `bao version` before
import/deployment. No scanner exception or organization package-visibility
change is used. Full upstream tests and all unfixed findings are outside this
bounded scan claim.

The current chart/image metadata and the public reproduction material contain
no operator tokens, recovery keys, source credentials or private kubeconfig.
