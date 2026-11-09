"""Build the reviewed attachment copy image on an operator machine and pack it for the target VM.

No registry, new credential or workflow is involved. The image still meets the bar of the published
images: one single-platform OCI index with provenance and SBOM, a Trivy scan with the same settings, the
Executor publisher's own blob, source and attestation verifier, and an isolated runtime probe. The
archive is written only after all of them pass. The platform is the target node's: linux/amd64 unless
--platform linux/arm64 is given. See docs/operations/t8-attachment-copy-job.md.
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import executor_image
from executor_image import Failure
from image_delivery.oci import verify_layout as verify_oci_layout

PYTHON = sys.version_info[:2]
# containerd does not start an index that has no manifest for the node's own architecture.
PLATFORMS = ("linux/amd64", "linux/arm64")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
IMAGE_NAME = "localhost/bravo-attachment-copy"
CONTEXT_DIRECTORY = "infra/envs/target-workloads"
DOCKERFILE = "attachment-copy.Dockerfile"
CONTEXT_FILES = (DOCKERFILE, "attachment-copy.requirements.txt", "attachment_copy.py", "attachment_stores.py",
                 "sync_attachments.py")
SBOM_GENERATOR = ("mirror.gcr.io/docker/buildkit-syft-scanner"
                  "@sha256:873a7458c3c8ac8e32c7b796c10e1600b07b963d2aa000a72a2d97aaaa2d6315")
# The Trivy version trivy-action v0.36.0 installs for publish-executor-image.yml.
TRIVY_IMAGE = "mirror.gcr.io/aquasec/trivy:0.70.0@sha256:be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e"
# --all-platforms keeps the attestations with the index. Import did not create the digest name when measured.
IMPORT_ARGUMENTS = ("images", "import", "--all-platforms", "--digests", "--base-name", IMAGE_NAME)
ARCHIVE_MEMBER = re.compile(r"oci-layout|index\.json|blobs/sha256/[a-f0-9]{64}")
RUN_TIMEOUT_SECONDS = 1200
PROBE = """import importlib.util, os, sys
sys.path.insert(0, '/app')
import boto3, psycopg, attachment_copy, attachment_stores, sync_attachments
if (os.getuid() != 10001 or os.getgid() != 10001 or psycopg.pq.version() <= 0 or
    any(importlib.util.find_spec(name) is not None for name in ('pip', 'setuptools'))):
    raise SystemExit('attachment_runtime_probe=fail')
print('attachment_runtime_probe=pass')
"""


def run(arguments, phase):
    """Return stdout. Tool output can carry paths or registry details, so a failure names only the phase."""
    result = subprocess.run(arguments, check=False, capture_output=True, timeout=RUN_TIMEOUT_SECONDS)
    if result.returncode:
        raise Failure(phase + "_failed")
    return result.stdout


def source_context(repo, source, destination):
    """Extract the build files of one reviewed commit, so local edits and other files never reach the image."""
    executor_image.source_sha(source)
    run(["git", "-C", str(repo), "merge-base", "--is-ancestor", source, "origin/develop"], "source_not_reviewed")
    archive = run(["git", "-C", str(repo), "archive", "--format=tar", f"{source}:{CONTEXT_DIRECTORY}", *CONTEXT_FILES],
                  "source_archive")
    with tarfile.open(fileobj=io.BytesIO(archive)) as stored:
        # Exactly these paths were requested, so a link or a missing file is the only way this differs.
        if sorted(m.name for m in stored.getmembers() if m.isfile()) != sorted(CONTEXT_FILES):
            raise Failure("source_archive_inventory_mismatch")
        destination.mkdir(parents=True)
        stored.extractall(destination, filter="data")


def pinned_base(dockerfile_text):
    bases = re.findall(r"(?m)^FROM\s+(.+)$", dockerfile_text)
    if len(bases) != 1 or not re.fullmatch(r"python:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}", bases[0]):
        raise Failure("base_image_not_pinned")
    return bases[0]


def build_command(source, base, context, layout, metadata, platform):
    return ["docker", "buildx", "build", "--platform", platform, "--provenance=mode=max",
            f"--sbom=generator={SBOM_GENERATOR}",
            # BuildKit leaves the attestation subject empty for an unnamed image, and the verifier refuses that.
            "-t", f"{IMAGE_NAME}:sha-{source}",
            "--label", f"org.opencontainers.image.source=https://github.com/{executor_image.REPOSITORY}",
            "--label", f"org.opencontainers.image.revision={source}",
            "--output", f"type=oci,dest={layout},tar=false", "--metadata-file", str(metadata),
            # The digest pins the bytes, so a public mirror can only supply the same base.
            # An operator's expired Docker Hub login would otherwise stop the build.
            "--build-context", f"docker.io/library/{base}=docker-image://mirror.gcr.io/library/{base}",
            "-f", str(context / DOCKERFILE), str(context)]


def scan_command(layout, output):
    return ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "TRIVY_CACHE_DIR=/tmp/trivy",
            "-v", f"{layout}:/layout:ro", "-v", f"{output}:/out", TRIVY_IMAGE, "image", "--input", "/layout",
            "--scanners", "vuln,secret", "--severity", "HIGH,CRITICAL", "--ignore-unfixed", "--format", "json",
            "--output", "/out/trivy.json", "--exit-code", "1", "--timeout", "10m"]


def probe_command(image, platform):
    return ["docker", "run", "--rm", "--platform", platform, "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "256m",
            "--entrypoint", "python3", image, "-c", PROBE]


def pack(layout, archive):
    """Tar only the OCI layout, without owner or time, so the same layout gives the same bytes."""
    blobs = sorted(path.name for path in (layout / "blobs/sha256").iterdir())
    with tarfile.open(archive, "w") as stored:
        for name in ("oci-layout", "index.json", *(f"blobs/sha256/{blob}" for blob in blobs)):
            path = layout / name
            member = tarfile.TarInfo(name)
            member.size = path.stat().st_size
            member.mode = 0o644
            with path.open("rb") as body:
                stored.addfile(member, body)


def verify_layout(layout, digest, source, platform):
    """The Executor publisher's verifier, for the one platform this image was built for."""
    return verify_oci_layout(layout, digest, source, repository=executor_image.REPOSITORY,
                             architecture=platform.removeprefix("linux/"))


def verify_archive(archive, digest, source, parent, platform):
    """Verify what will actually be transferred. Members are checked before anything is extracted."""
    with tempfile.TemporaryDirectory(dir=parent) as temporary, tarfile.open(archive) as stored:
        names = [member.name for member in stored.getmembers()]
        if (len(names) != len(set(names)) or not all(member.isfile() for member in stored.getmembers())
                or not all(ARCHIVE_MEMBER.fullmatch(name) for name in names)):
            raise Failure("unexpected_archive_member")
        stored.extractall(temporary, filter="data")
        return verify_layout(Path(temporary), digest, source, platform)


def remove_loaded_image(tag):
    try:
        run(["docker", "image", "rm", tag], "runtime_cleanup")
    except Failure:
        pass  # A leftover local tag is harmless and must not hide the probe's result.


def runtime_probe(archive, digest, source, platform):
    tag = f"{IMAGE_NAME}:sha-{source}"
    run(["docker", "load", "--input", str(archive)], "runtime_load")
    try:
        output = run(probe_command(f"{IMAGE_NAME}@{digest}", platform), "runtime_probe")
    finally:
        remove_loaded_image(tag)
    if b"attachment_runtime_probe=pass" not in output:
        raise Failure("runtime_probe_failed")


def build(repo, source, output_directory, platform):
    executor_image.source_sha(source)
    # Both platforms of one commit carry the same tag, so only the file name tells them apart before import.
    stem = f"attachment-copy-{source[:12]}-{platform.removeprefix('linux/')}"
    archive = output_directory / f"{stem}.tar"
    receipt_path = output_directory / f"{stem}.receipt.json"
    if archive.exists() or receipt_path.exists():
        raise Failure("output_must_be_new")
    with tempfile.TemporaryDirectory(prefix="attachment-image-", dir=output_directory) as temporary:
        work = Path(temporary)
        context, layout, metadata, scan = work / "context", work / "layout", work / "metadata.json", work / "scan"
        source_context(repo, source, context)
        base = pinned_base((context / DOCKERFILE).read_text())
        run(build_command(source, base, context, layout, metadata, platform), "image_build")
        digest = json.loads(metadata.read_text()).get("containerimage.digest")
        scan.mkdir()
        run(scan_command(layout, scan), "image_scan")
        summary = executor_image.scan_summary(json.loads((scan / "trivy.json").read_text()))
        inventory = verify_layout(layout, digest, source, platform)
        staged = work / "image.tar"
        pack(layout, staged)
        if verify_archive(staged, digest, source, work, platform) != inventory:
            raise Failure("archive_inventory_mismatch")
        runtime_probe(staged, digest, source, platform)
        with staged.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        receipt = {"source_repository": executor_image.REPOSITORY, "source_sha": source, "base_image": base,
                   "image": f"{IMAGE_NAME}@{digest}", "digest": digest, "platform": platform,
                   "runtime_user": "10001:10001", "archive_sha256": checksum, "archive_bytes": staged.stat().st_size,
                   "verified_blob_count": len(inventory), "scan": summary, "scanned_before_packing": True,
                   "provenance_and_sbom_verified": True, "isolated_runtime_probe_verified": True,
                   "built_in_ci": False, "target_import_verified": False, "target_job_verified": False}
        os.link(staged, archive)  # Atomic publication without overwriting an existing artifact.
    with receipt_path.open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Full commit already merged into develop")
    parser.add_argument("--output-dir", required=True, type=Path, help="Private directory outside the repository")
    parser.add_argument("--platform", choices=PLATFORMS, default="linux/amd64", help="The target node's platform")
    args = parser.parse_args()
    try:
        if PYTHON < (3, 12):
            raise Failure("python_3_12_required")
        os.umask(0o077)
        args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        receipt = build(REPOSITORY_ROOT, args.source, args.output_dir, args.platform)
    except Failure as failure:
        print(json.dumps({"attachment_image": "NOT_VERIFIED", "error_code": str(failure)}))
        return 1
    except (OSError, ValueError, KeyError, IndexError, TypeError, tarfile.TarError, subprocess.SubprocessError):
        print(json.dumps({"attachment_image": "NOT_VERIFIED", "error_code": "invalid_local_state"}))
        return 1
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
