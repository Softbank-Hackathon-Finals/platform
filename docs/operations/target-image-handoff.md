# T30 private image and runtime configuration handoff

This procedure prepares the existing reviewed demo-chat image for an independently
owned local amd64 development VM. It does not install a VM, change a cluster,
issue credentials, or make the unchanged application AWS-independent.

The source, index and linux/amd64 child digests are in
[`app-image.json`](../../infra/envs/target-workloads/app-image.json), the one place
that names the image. Read the reference with:

```sh
APP_IMAGE=$(python3 infra/envs/target-workloads/application.py --print-image)
```

This export serves amd64 nodes only. For the arm64 target node, see
[arm64 target node](#arm64-target-node).

The earlier `ce5727e` image and its export evidence do not send `X-Bravo-Env`.
No new application build or registry visibility change is part of this handoff.

**ECR already holds the pinned image.** The publication record on demo-chat `main`
was updated by demo-chat #19, and `transfer-private-image`
[run 41815292046](https://github.com/Softbank-Hackathon-Finals/demo-chat/actions/runs/41815292046)
copied it on 2026-11-04 at 19:36 KST, with identical index and attestation blobs and a
verified destination pull. It verified the pull from CI, not from a cluster. For a new pin,
the T22 owner first updates that record under `docs/evidence/image-publication-2026-11-03/`,
because the workflow validates its inputs against it, then a person with Actions access
runs the workflow.

**The development VM has it too.** Per the owner's record on #210, the bundle was
delivered on 2026-11-04 at 22:36 KST and registered in the VM's containerd at 23:05,
and the index and linux/amd64 manifest were compared again at 23:07. That is the
owner's record; this document did not verify it directly. After this change merges,
the VM owner compares the merged digest and deploys. The steps below remain for
another VM or a new pin.

## Export without handing registry credentials to the VM

If `skopeo` is missing, install it first (`brew install skopeo`). Use the existing AWS operator identity, `skopeo` and the official
`docker-credential-ecr-login` already installed on the operator machine:

```sh
python3 scripts/export_target_image.py \
  --contract "$PRIVATE_REGISTRY_CONTRACT" \
  --archive "$PRIVATE_IMAGE_ARCHIVE" \
  --receipt "$PRIVATE_EXPORT_RECEIPT"
```

Create the private contract outside the repository, from the pin and your own AWS account:

```sh
python3 - "$PRIVATE_REGISTRY_CONTRACT" "$(aws sts get-caller-identity --query Account --output text)" <<'PY'
import json, pathlib, sys
pin = json.loads(pathlib.Path("infra/envs/target-workloads/app-image.json").read_text())
account = sys.argv[2]
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "account_id": account, "region": "ap-northeast-2",
    "repository_url": f"{account}.dkr.ecr.ap-northeast-2.amazonaws.com/bravo/demo-chat",
    "source_sha": pin["source_sha"], "digest": pin["index_digest"],
    "status": "image_copied_and_ci_pull_verified"}))
PY
```

The private contract contains the expected AWS account, exact repository,
source/digest and successful prior-copy status. The tool checks the caller's
account and refuses other sources/repositories. The helper obtains temporary
ECR auth over its internal pipe, with `AWS_ECR_DISABLE_CACHE=true`; no login,
password argument, token file, new AWS key or Kubernetes pull Secret is used.
The temporary auth configuration contains only an exact-host helper reference.
Helper logs stay in the private temporary directory and are removed afterwards.
The user's Docker and AWS configuration are not changed.

Skopeo copies **all** manifests with `--preserve-digests`. Before publishing a
new archive, the tool verifies every blob hash and descriptor size, the complete
reachable graph (including attestations), the exact index/amd64 manifest,
and the image config's source revision, architecture and UID10001. Archive
verification never extracts files and rejects extra/traversal/link entries.
Graph verification, checksum and size use the same open file. The verifier
refuses to issue a receipt if the archive path is replaced or the file changes
in place during verification. The receiver must still compare the archive with
the receipt checksum immediately before import; a receipt does not freeze a file
against subsequent changes.
Existing artifacts/receipts are not overwritten. Keep the archive and private
registry contract outside Git; publish only the bounded receipt if needed.

The VM owner transfers only the archive and its expected SHA-256 through their
verified SSH host-key/port contract. Verify bytes at both ends. The verifier can
run without AWS credentials or network:

```sh
python3 scripts/export_target_image.py --verify-only \
  --archive "$PRIVATE_IMAGE_ARCHIVE" --receipt "$NEW_PRIVATE_VERIFY_RECEIPT"
```

For the owner to adapt to the installed RKE2/containerd version, a typical
import is:

```sh
sudo /var/lib/rancher/rke2/bin/ctr \
  --address /run/k3s/containerd/containerd.sock --namespace k8s.io \
  images import --all-platforms --digests \
  --base-name ghcr.io/softbank-hackathon-finals/demo-chat "$PRIVATE_IMAGE_ARCHIVE"
```

Check that the exact index reference exists in `k8s.io`, and the amd64 content
is unpacked. Import can retain the OCI annotation's tag
`ghcr.io/softbank-hackathon-finals/demo-chat:sha-<source>`
without creating the exact `$APP_IMAGE`
name, even with `--base-name` and `--digests`. A `Never` Pod using the absent
name will fail although the index bytes are present.

Read `ctr images list` in the same socket/namespace. If the expected name is
absent, select the imported reference whose descriptor digest is **exactly the
original index digest** above, not the amd64 child digest. Set
`VERIFIED_IMPORTED_REFERENCE` to that observed reference and add only the alias:

```sh
sudo /var/lib/rancher/rke2/bin/ctr \
  --address /run/k3s/containerd/containerd.sock --namespace k8s.io \
  images tag "$VERIFIED_IMPORTED_REFERENCE" \
  "$APP_IMAGE"
```

Read the exact destination name and descriptor digest again before launching
the packaging Pod. If the expected name already has that digest, skip tagging;
if it points to a different digest or the source descriptor cannot be verified,
stop. Do not use `--force`, rebuild or change the archive to make a name match.
An alias only adds a containerd image name; it does not prove that a Pod ran.

Use the full original image reference plus index digest and
`imagePullPolicy: Never` for this bounded offline development probe. A missing
local image must fail instead of falling back to an unauthenticated private
registry request. `Always` requires registry authentication and is not the
archive-only path. Import on every participating node; eviction/new nodes need
a repeat verified import. This is not a general production registry solution.

The owner must record import/image digest and an actual restricted non-root
packaging Pod result before claiming target runtime success. Export success
alone proves neither import nor application startup. No other session's VM,
containerd namespace, SSH keys or active workloads may be reused implicitly.

## arm64 target node

The target node changed from x86 to arm64 around 05:20 KST on 2026-11-06. The
publication above is linux/amd64 only. The arm64 node instead runs a local build
already imported into its containerd. `app-image.json` records it under `arm64`,
and `application.py --arch arm64` renders it with
`nodeSelector: kubernetes.io/arch: arm64` and `imagePullPolicy: Never`.

Its source was checked by file content: the 17 files under `/app/app` in the
running image have the same SHA-256 as `app/` at the entry's `source_sha`. The
builder's record adds how it was built: those app files with a local Dockerfile
adjusted for arm64, so there is no single demo-chat build commit. The entry keeps
that Dockerfile's SHA-256, the Python and uv base digests and the build time
instead. This record was not reproduced by a rebuild. To repeat the file check
with read access only, from the platform checkout and with a demo-chat checkout in
`$DEMO_CHAT`:

```sh
kubectl -n target-dev exec deployment/demo-chat -c app -- python -c '
import hashlib, pathlib
for p in sorted(pathlib.Path("/app/app").rglob("*")):
    if p.is_file() and "__pycache__" not in p.parts:
        print(hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to("/app"))' > live-app.sha
SOURCE=$(python3 -c 'import json; print(json.load(open("infra/envs/target-workloads/app-image.json"))["arm64"]["source_sha"])')
git -C "$DEMO_CHAT" ls-tree -r --name-only "$SOURCE" app/ | while read -r p; do
  printf '%s %s\n' "$(git -C "$DEMO_CHAT" show "$SOURCE:$p" | shasum -a 256 | cut -d' ' -f1)" "$p"
done | sort -k2 | diff - <(sort -k2 live-app.sha) && echo "same app files"
```

Equal app files do not prove equal dependencies or base layers. A rebuilt or
re-imported arm64 image is another digest; record it in `app-image.json` with the
same check before rendering it.

## Unchanged application configuration and the S2/T9 gap

These links are the `ce5727e` source that this analysis first read. The pinned image
keeps the same startup call and adds the `ATTACHMENT_UPLOAD_ENABLED` setting below:
[config.py](https://github.com/Softbank-Hackathon-Finals/demo-chat/blob/ce5727eb3e3d2355077afdaadcb1a722aba9c7cd/app/config.py),
[main.py](https://github.com/Softbank-Hackathon-Finals/demo-chat/blob/ce5727eb3e3d2355077afdaadcb1a722aba9c7cd/app/main.py),
[db_secret.py](https://github.com/Softbank-Hackathon-Finals/demo-chat/blob/ce5727eb3e3d2355077afdaadcb1a722aba9c7cd/app/db_secret.py),
[storage.py](https://github.com/Softbank-Hackathon-Finals/demo-chat/blob/ce5727eb3e3d2355077afdaadcb1a722aba9c7cd/app/storage.py).

| Setting/path | Current behavior and prerequisite |
|---|---|
| `APP_ENV` | Display and PostgreSQL application name only; does not select a secret/storage implementation. |
| `DB_HOST`, `DB_PORT`, `DB_NAME` | Target PgBouncer endpoint, port5432 and chat database. Before cutover, that pooler points to the source DB through the owner's cross-environment route. The target DB initially receives replication, not business writes. |
| `DB_SECRET_ID` | Required AWS Secrets Manager identifier. Startup unconditionally calls `GetSecretValue` and expects username/password in `SecretString`; it fails before readiness if lookup fails. |
| `DB_USER`, `DB_PASSWORD`, OpenBao/ESO-injected DB values | Not consumed by this image. Merely mounting or injecting them cannot replace the unconditional SDK call. |
| PostgreSQL TLS | libpq settings such as `PGSSLMODE=verify-full` and `PGSSLROOTCERT` must be paired with a trusted CA mount and a certificate matching the target pooler's hostname. Application DB connection must not use the pooler control/admin identity. |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_FORCE_PATH_STYLE=true` | Support an S3-compatible endpoint, but require the T9-owned SeaweedFS service, bucket and signing identity. The existing code uses boto3's default credential chain; endpoint configuration alone does not provide authorization. |
| Both AWS clients | Secrets Manager and S3 share the default credential chain. Setting SeaweedFS access-key environment variables would also affect the AWS Secrets Manager call. Do not copy an operator's AWS admin credentials into the target to conceal this dependency. |
| `ADMIN_TOKEN` | Consumed from environment; T9/owner must provide its secret reference. No value is generated or transmitted here. |
| `ATTACHMENT_UPLOAD_ENABLED=false` | Refuses uploads before cutover. An upload's object would otherwise stay in target storage while its row goes to the source DB through the pooler. |
| `WRITE_BLOCKED=true` | Required initial target application state. Leave source/target write control to the cutover owner; do not independently open both environments. |

The source AGENTS explicitly keeps the direct Secrets Manager dependency as
S2's **code-change** demonstration. Preserve the original image. Full
AWS-independent target startup requires the reviewed S2 change and a new approved
image digest, plus T9 OpenBao/ESO/SeaweedFS contracts. Do not inject Python code,
monkey-patch the application, replace its entrypoint with a secret shim, or deploy
a compatibility proxy and label it unchanged-image exit success.

Until those dependencies are ready, a restricted packaging/import probe and
separate DB replication checks are valid progress. They are not T30 application
read/write completion, T9 completion, or a successful cutover. Temporary registry
auth is kept on the operator side and cannot solve application Secret/S3 auth.

For development Edge measurement only, [ADR-0017](../adr/0017-target-app-secrets-manager-compat.md)
runs a loopback compatibility container beside the unchanged app. It does not change
the rule above, and Exit Drill judges such a Pod UNKNOWN from the Pod itself. See
[target-workloads](../../infra/envs/target-workloads/README.md#개발-측정용-앱).

Official tool references:
[ECR authentication](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html),
[ECR helper cache controls](https://github.com/awslabs/amazon-ecr-credential-helper#amazon-ecr-docker-credential-helper),
[containers credential helpers](https://github.com/containers/image/blob/main/docs/containers-auth.json.5.md),
[containerd OCI import options](https://github.com/containerd/containerd/blob/main/cmd/ctr/commands/images/import.go).
