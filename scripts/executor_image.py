"""Publish one reviewed Executor image; never deploy it or print private identifiers."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

from image_delivery.oci import Failure, digest_value, scan_summary, source_sha
from image_delivery.oci import verify_layout as verify_oci_layout

REPOSITORY = "Softbank-Hackathon-Finals/platform"
COMPONENT = "services/cutover-executor"
REGION = "ap-northeast-2"
ECR_NAME = "bravo/cutover-executor"
PROBE = """import os, importlib.util, psycopg, bravo_cutover.server
if (os.getuid() != 10001 or os.getgid() != 10001 or psycopg.pq.version() <= 0 or
    any(importlib.util.find_spec(name) is not None for name in ('pip', 'setuptools'))):
    raise SystemExit('executor_runtime_probe=fail')
print('executor_runtime_probe=pass')
"""




def run(args, phase):
    result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise Failure(phase + "_failed")
    return result.stdout


def workflow_boundary(env):
    expected = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REPOSITORY_ID": "1443045223",
                "GITHUB_REPOSITORY_OWNER_ID": "350259663", "GITHUB_REF": "refs/heads/develop",
                "GITHUB_EVENT_NAME": "workflow_dispatch"}
    if any(env.get(key) != value for key, value in expected.items()):
        raise Failure("workflow_identity_or_branch_mismatch")






def publication_tag(source, env):
    source_sha(source)
    run_id, attempt = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", "")
    if not all(re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, attempt)):
        raise Failure("publication_run_identity_missing")
    return f"sha-{source}-run-{run_id}-{attempt}"


def prepare(source, destination):
    source_sha(source)
    run(["git", "merge-base", "--is-ancestor", source, "origin/develop"], "source_not_reviewed")
    tree = source + ":" + COMPONENT
    tree_sha = run(["git", "rev-parse", tree], "missing_executor_source").strip()
    entries = run(["git", "ls-tree", "-rz", tree], "source_inventory").split("\0")
    names = []
    for entry in filter(None, entries):
        metadata, name = entry.split("\t", 1)
        path = PurePosixPath(name)
        if metadata.split()[0] not in {"100644", "100755"}:
            raise Failure("source_symlink_or_submodule")
        if (path.is_absolute() or ".." in path.parts or
            any(p in {".git", ".venv", ".aws", ".kube", ".state"} for p in path.parts) or
            ((path.name == ".env" or path.name.startswith(".env.")) and path.name != ".env.example") or
            path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".tfstate"}):
            raise Failure("credential_or_runtime_source_file")
        names.append(name)
    if not {"Dockerfile", "pyproject.toml", "uv.lock", "src/bravo_cutover/server.py"}.issubset(names):
        raise Failure("executor_publication_contract_missing")
    result = subprocess.run(["git", "archive", "--format=tar", tree], check=False, capture_output=True, timeout=60)
    if result.returncode:
        raise Failure("source_archive_failed")
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        archive.extractall(destination, filter="data")
    extracted = sorted(str(p.relative_to(destination)) for p in destination.rglob("*") if p.is_file())
    if extracted != sorted(names):
        raise Failure("source_archive_inventory_mismatch")
    return {"source_sha": source, "component": COMPONENT, "source_tree_sha": tree_sha,
            "source_archive_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "files": names, "local_untracked_files_included": False}




def verify_layout(layout, digest, source):
    return verify_oci_layout(layout, digest, source, repository=REPOSITORY)


def destination_boundary(repository, account):
    if not re.fullmatch(r"[0-9]{12}", account or ""):
        raise Failure("expected_account_missing")
    if repository != f"{account}.dkr.ecr.{REGION}.amazonaws.com/{ECR_NAME}":
        raise Failure("executor_repository_mismatch")


def verify(root, source, digest):
    manifest = json.loads((root / "executor-source-manifest.json").read_text())
    if manifest.get("source_sha") != source or manifest.get("component") != COMPONENT:
        raise Failure("source_manifest_mismatch")
    summary = scan_summary(json.loads((root / "executor-trivy.json").read_text()))
    inventory = verify_layout(root / "executor-layout", digest, source)
    (root / "executor-scan-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return inventory


def existing_digest(tag):
    result = subprocess.run(["aws", "ecr", "describe-images", "--repository-name", ECR_NAME,
        "--image-ids", "imageTag=" + tag, "--region", REGION, "--output", "json"],
        check=False, capture_output=True, text=True, timeout=60)
    if result.returncode:
        if "(ImageNotFoundException)" in result.stderr:
            return None
        raise Failure("destination_inventory_failed")
    images = json.loads(result.stdout)["imageDetails"]
    if len(images) != 1:
        raise Failure("destination_inventory_ambiguous")
    return digest_value(images[0]["imageDigest"])


def publish(root, source, digest, repository, account):
    destination_boundary(repository, account)
    tag = publication_tag(source, os.environ)
    original = verify(root, source, digest)
    identity = json.loads(run(["aws", "sts", "get-caller-identity", "--region", REGION,
                              "--output", "json"], "publisher_identity"))
    arn = identity.get("Arn", "").split(":", 5)
    role_session = arn[-1].split("/")
    if (identity.get("Account") != account or arn[:5] != ["arn", "aws", "sts", "", account] or
        len(role_session) != 3 or role_session[:2] != ["assumed-role", "bravo-executor-image-publisher"] or
        not role_session[2]):
        raise Failure("publisher_identity_mismatch")
    auth = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker"))) / "config.json"
    existing = existing_digest(tag)
    if existing is None:
        run(["skopeo", "copy", "--all", "--preserve-digests", "--dest-authfile", str(auth),
             "oci:" + str(root / "executor-layout"), "docker://" + repository + ":" + tag], "publish")
    elif existing != digest:
        raise Failure("immutable_tag_conflict")
    image = repository + "@" + digest
    target = root / "executor-destination"
    run(["skopeo", "copy", "--all", "--preserve-digests", "--src-authfile", str(auth),
         "docker://" + image, "oci:" + str(target)], "destination_download")
    if verify_layout(target, digest, source) != original:
        raise Failure("destination_inventory_mismatch")
    run(["docker", "pull", "--platform", "linux/amd64", image], "runtime_pull")
    run(["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
         "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
         "--pids-limit", "64", "--memory", "256m", "--entrypoint", "python", image, "-c", PROBE], "runtime_probe")
    receipt = {"source_repository": REPOSITORY, "source_sha": source, "component": COMPONENT,
        "digest": digest, "tag": tag, "repository_name": ECR_NAME, "registry": "aws-ecr-private",
        "platform": "linux/amd64", "runtime_user": "10001:10001", "oidc_session_seconds": 900,
        "scanned_before_publish": True, "index_and_attestation_blobs_identical": True,
        "verified_blob_count": len(original), "destination_pull_verified": True,
        "isolated_runtime_probe_verified": True, "cluster_pull_verified": False,
        "api_startup_verified": False, "cutover_performed": False}
    (root / "executor-image-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    if sys.version_info < (3, 12):
        raise Failure("python_3_12_required")
    os.umask(0o077)
    workflow_boundary(os.environ)
    root, source = Path(os.environ["RUNNER_TEMP"]), source_sha(os.environ["SOURCE_SHA"])
    phase = sys.argv[1]
    if phase == "prepare":
        manifest = prepare(source, root / "executor-source")
        (root / "executor-source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    elif phase == "verify":
        verify(root, source, digest_value(os.environ["BUILD_DIGEST"]))
    elif phase == "publish":
        print(json.dumps(publish(root, source, digest_value(os.environ["BUILD_DIGEST"]),
            os.environ["EXECUTOR_ECR_REPOSITORY"], os.environ["EXPECTED_ACCOUNT_ID"])))
    else:
        raise Failure("unknown_phase")
    print(json.dumps({"phase": phase, "status": "PASS"}))


if __name__ == "__main__":
    try:
        main()
    except (Failure, OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"publication": "NOT_VERIFIED",
                          "error_code": str(error) if isinstance(error, Failure) else "invalid_local_state"}))
        raise SystemExit(1) from None
