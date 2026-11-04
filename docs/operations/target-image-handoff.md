# T30 private image and runtime configuration handoff

This procedure prepares the existing reviewed demo-chat image for an independently
owned local amd64 development VM. It does not install a VM, change a cluster,
issue credentials, or make the unchanged application AWS-independent.

The fixed source is `8d82a9241d3894737778a77d58349e1aee54eca1`; the index is
`sha256:ca5ce1b24f1deb2e7346643860ff0bb151409c1acec503c105f56d9043a389b6` and
linux/amd64 child is
`sha256:f9f48872b57d7c94eb01c81622feca66e7fcdcaec304b5c879682777606ac24c`, from
[demo-chat run41803312129](https://github.com/Softbank-Hackathon-Finals/demo-chat/actions/runs/41803312129).
The earlier `ce5727e` image and its export evidence do not send `X-Bravo-Env`.
No new application build or registry visibility change is part of this handoff.

**Before exporting**, a person with Actions access runs demo-chat
`transfer-private-image` on `main` with that source SHA and index digest, so ECR
holds the same bytes. As of 2026-11-04 18:05 KST its last run was for `dc9bece`, so
this image is not yet in ECR. The export tool has not yet run against this image;
only its verifier code and unit tests changed for it.

## Export without handing registry credentials to the VM

Use the existing AWS operator identity, `skopeo` and the official
`docker-credential-ecr-login` already installed on the operator machine:

```sh
python3 scripts/export_target_image.py \
  --contract "$PRIVATE_REGISTRY_CONTRACT" \
  --archive "$PRIVATE_IMAGE_ARCHIVE" \
  --receipt "$PRIVATE_EXPORT_RECEIPT"
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
`ghcr.io/softbank-hackathon-finals/demo-chat:sha-8d82a9241d3894737778a77d58349e1aee54eca1`
without creating the exact `ghcr.io/softbank-hackathon-finals/demo-chat@sha256:ca5ce1b24f1deb2e7346643860ff0bb151409c1acec503c105f56d9043a389b6`
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
  ghcr.io/softbank-hackathon-finals/demo-chat@sha256:ca5ce1b24f1deb2e7346643860ff0bb151409c1acec503c105f56d9043a389b6
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

## Unchanged application configuration and the S2/T9 gap

The actual fixed source is authoritative:
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

For development Edge measurement only, [ADR-0016](../adr/0016-target-app-secrets-manager-compat.md)
runs a loopback compatibility container beside the unchanged app. It is labelled
as not exit evidence and does not change the rule above. See
[target-workloads](../../infra/envs/target-workloads/README.md#개발-측정용-앱).

Official tool references:
[ECR authentication](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html),
[ECR helper cache controls](https://github.com/awslabs/amazon-ecr-credential-helper#amazon-ecr-docker-credential-helper),
[containers credential helpers](https://github.com/containers/image/blob/main/docs/containers-auth.json.5.md),
[containerd OCI import options](https://github.com/containerd/containerd/blob/main/cmd/ctr/commands/images/import.go).
