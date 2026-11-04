"""Export the reviewed private image as verified OCI bytes, without exporting auth."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

SOURCE = "ce5727eb3e3d2355077afdaadcb1a722aba9c7cd"
INDEX = "sha256:001adc35a9fc767566552fb55c3c19a67b7c5efc7231fb02dc28cdeace99d0d4"
AMD64 = "sha256:bd1cd57a1c3dda9bb2795696167e242fc3b79a80eefb40d068a4124bcc66f384"
IMAGE = "ghcr.io/softbank-hackathon-finals/demo-chat"
REGION = "ap-northeast-2"


class Failure(RuntimeError):
    pass


def run(args, phase, env=None):
    result = subprocess.run(args, check=False, capture_output=True, timeout=900, env=env)
    if result.returncode:
        # Provider/registry output can contain private identifiers or credentials.
        raise Failure(phase + "_failed")
    return result.stdout


def validate_contract(contract):
    account = contract.get("account_id", "")
    if not re.fullmatch(r"[0-9]{12}", account):
        raise Failure("account_boundary_missing")
    repository = f"{account}.dkr.ecr.{REGION}.amazonaws.com/bravo/demo-chat"
    if any(contract.get(key) != value for key, value in {
        "region": REGION, "repository_url": repository, "source_sha": SOURCE, "digest": INDEX,
        "status": "image_copied_and_ci_pull_verified",
    }.items()):
        raise Failure("reviewed_registry_contract_mismatch")
    return account, repository


def _verify_archive_stream(stream):
    """Read and hash without extracting any archive member onto the filesystem."""
    with tarfile.open(fileobj=stream) as archive:
        members = {}
        inventory = {}
        for member in archive.getmembers():
            if member.isdir() and member.name in ("blobs", "blobs/sha256"):
                continue
            if not member.isfile() or member.name in members or not (
                member.name in ("index.json", "oci-layout")
                or re.fullmatch(r"blobs/sha256/[a-f0-9]{64}", member.name)
            ):
                raise Failure("unexpected_archive_member")
            members[member.name] = member
            if member.name.startswith("blobs/"):
                digest = hashlib.sha256()
                stream = archive.extractfile(member)
                for chunk in iter(lambda stream=stream: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                key = member.name.rsplit("/", 1)[1]
                if digest.hexdigest() != key:
                    raise Failure("blob_hash_mismatch")
                inventory["sha256:" + key] = member.size

        def document(name):
            if name not in members or members[name].size > 4 * 1024 * 1024:
                raise Failure("manifest_missing_or_unbounded")
            return json.load(archive.extractfile(members[name]))

        if document("oci-layout") != {"imageLayoutVersion": "1.0.0"}:
            raise Failure("oci_layout_invalid")
        wrapper = document("index.json")
        if len(wrapper.get("manifests", [])) != 1 or wrapper["manifests"][0].get("digest") != INDEX:
            raise Failure("original_index_not_preserved")
        if wrapper["manifests"][0].get("annotations", {}).get("org.opencontainers.image.ref.name") != IMAGE + ":sha-" + SOURCE:
            raise Failure("unexpected_import_reference")
        root = document("blobs/sha256/" + INDEX.removeprefix("sha256:"))
        platforms = [m for m in root["manifests"] if m.get("platform", {}).get("architecture") == "amd64"
                     and m.get("platform", {}).get("os") == "linux"]
        if len(platforms) != 1 or platforms[0]["digest"] != AMD64:
            raise Failure("reviewed_amd64_manifest_missing")
        pending = [wrapper]
        visited = set()
        while pending:
            obj = pending.pop()
            descriptors = obj.get("manifests", []) + obj.get("layers", [])
            if "config" in obj:
                descriptors.append(obj["config"])
            for item in descriptors:
                digest = item["digest"]
                if inventory.get(digest) != item["size"]:
                    raise Failure("descriptor_missing_or_size_mismatch")
                if digest in visited:
                    continue
                visited.add(digest)
                if "manifest" in item["mediaType"] or "index" in item["mediaType"]:
                    pending.append(document("blobs/sha256/" + digest.removeprefix("sha256:")))
        if visited != set(inventory) or len(inventory) != 15:
            raise Failure("blob_graph_incomplete_or_extra")
        manifest = document("blobs/sha256/" + AMD64.removeprefix("sha256:"))
        config = document("blobs/sha256/" + manifest["config"]["digest"].removeprefix("sha256:"))
        if (config.get("architecture"), config.get("os"), config.get("config", {}).get("User")) != (
            "amd64", "linux", "10001"
        ) or config["config"].get("Labels", {}).get("org.opencontainers.image.revision") != SOURCE:
            raise Failure("runtime_image_identity_mismatch")
        return inventory


def verified_archive_receipt(path):
    """Bind verification, checksum and size to one unchanged open file."""
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)

    path = pathlib.Path(path)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        inventory = _verify_archive_stream(stream)
        stream.seek(0)
        checksum = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
        # fstat detects in-place writes; stat also rejects replacement of the
        # supplied path while the original descriptor remains open.
        if (identity(os.fstat(stream.fileno())) != identity(before)
                or identity(path.stat()) != identity(before)):
            raise Failure("archive_changed_during_verification")
        return inventory, checksum.hexdigest(), before.st_size


def verify_archive(path):
    return verified_archive_receipt(path)[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=pathlib.Path)
    parser.add_argument("--archive", required=True, type=pathlib.Path)
    parser.add_argument("--receipt", required=True, type=pathlib.Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.receipt.exists():
        raise Failure("receipt_exists_choose_new_path")
    if not args.verify_only:
        if args.contract is None or args.archive.exists() or args.archive.is_symlink():
            raise Failure("contract_required_and_archive_must_be_new")
        account, repository = validate_contract(json.loads(args.contract.read_text()))
        identity = json.loads(run(["aws", "sts", "get-caller-identity", "--region", REGION,
                                   "--output", "json", "--no-cli-pager"], "operator_identity"))
        if identity.get("Account") != account:
            raise Failure("operator_account_mismatch")
        if not shutil.which("docker-credential-ecr-login"):
            raise Failure("existing_official_ecr_helper_missing")
        args.archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="target-image-", dir=args.archive.parent) as temporary:
            work = pathlib.Path(temporary)
            # This file contains a helper reference only, never an auth token.
            auth = work / "auth.json"
            auth.write_text(json.dumps({"auths": {}, "credHelpers": {repository.split("/")[0]: "ecr-login"}}))
            env = {**os.environ, "AWS_REGION": REGION, "AWS_DEFAULT_REGION": REGION,
                   "AWS_ECR_DISABLE_CACHE": "true", "AWS_ECR_CACHE_DIR": str(work / "ecr")}
            run(["skopeo", "copy", "--quiet", "--all", "--preserve-digests", "--src-authfile", str(auth),
                 "docker://" + repository + "@" + INDEX,
                 "oci:" + str(work / "layout") + ":" + IMAGE + ":sha-" + SOURCE], "private_image_download", env)
            if any(p.name == "cache.json" for p in work.rglob("*")):
                raise Failure("unexpected_registry_token_cache")
            staged = work / "image.tar"
            with tarfile.open(staged, "w") as archive:
                for item in sorted((work / "layout").rglob("*")):
                    if item.is_symlink():
                        raise Failure("unexpected_layout_symlink")
                    if item.is_file():
                        archive.add(item, arcname=str(item.relative_to(work / "layout")), recursive=False)
            verify_archive(staged)
            os.link(staged, args.archive)  # Atomic publication without overwriting an existing artifact.
    inventory, checksum, archive_bytes = verified_archive_receipt(args.archive)
    receipt = {"observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "verifier_source_sha256": hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
               "source_sha": SOURCE, "image": IMAGE, "index_digest": INDEX, "amd64_digest": AMD64,
               "archive_sha256": checksum, "archive_bytes": archive_bytes,
               "verified_blob_count": len(inventory), "all_blob_hashes_and_descriptors_verified": True,
               "registry_credentials_exported": False, "new_long_lived_credentials_created": False, "temporary_ecr_auth_used": not args.verify_only,
               "registry_auth_disk_cache_disabled": None if args.verify_only else True,
               "verification_only": args.verify_only, "target_import_verified": False,
               "target_application_startup_verified": False, "aws_free_database_credentials_supported": False}
    args.receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.receipt.open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (Failure, OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"image_export": "NOT_VERIFIED", "error_code": str(error) if isinstance(error, Failure) else "invalid_export_state"}))
        raise SystemExit(1) from None
