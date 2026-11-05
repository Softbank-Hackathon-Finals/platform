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
| `rehearsal-policy` | HCL for policy `rehearsal-app-reader`: read `secret/data/bravo/rehearsal/*` only | No `bravo/target/*`, no writes, no token creation |
| `rehearsal-auth-role` | Body for `auth/kubernetes/role/rehearsal-app-reader` | ServiceAccount `rehearsal-eso-reader` in namespaces selected by the rehearsal label; audience `openbao`; TTL 300/900 s |
| `rehearsal-delivery --namespace rehearsal-<id> --run-id <run>` | ServiceAccount + SecretStore + ExternalSecret projecting `bravo/rehearsal/<run>/database` into Secret **`target-app-credentials`** (same name as `target-dev`) | One namespace per attempt; `deletionPolicy: Delete` so the Secret goes with the namespace |
| `rehearsal-init-container --run-id <run>` | An init container that logs in with the reader token and writes `credentials.json` itself | Works while ESO stays scoped (below) |

The T15 runner writes the synthetic values to `bravo/rehearsal/<run>/database` (key
`credentials_json`) with its own OpenBao identity before the attempt and deletes the version
afterwards; this renderer never carries a value.

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
