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


class Failure(RuntimeError):
    pass


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


def source_sha(value):
    if not re.fullmatch(r"[a-f0-9]{40}", value or ""):
        raise Failure("invalid_source_sha")
    return value


def digest_value(value):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value or ""):
        raise Failure("invalid_image_digest")
    return value


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


def scan_summary(report):
    if not isinstance(report, dict):
        raise Failure("scan_report_not_object")
    results = report.get("Results")
    if not isinstance(results, list) or not results or not all(
        isinstance(item, dict) and isinstance(item.get("Type"), str) for item in results
    ):
        raise Failure("scan_coverage_missing")
    types = {item.get("Type") for item in results}
    if not {"debian", "python-pkg"}.issubset(types):
        raise Failure("executor_package_coverage_missing")
    if any(item.get(key) is not None and not isinstance(item[key], list)
           for item in results for key in ("Vulnerabilities", "Secrets")):
        raise Failure("scan_findings_malformed")
    vulnerabilities = sum(len(item.get("Vulnerabilities") or []) for item in results)
    secrets = sum(len(item.get("Secrets") or []) for item in results)
    if vulnerabilities or secrets:
        raise Failure("image_scan_has_findings")
    return {"scanners": ["vuln", "secret"], "severity": ["HIGH", "CRITICAL"],
            "ignore_unfixed": True, "package_types": sorted(types),
            "vulnerability_count": 0, "secret_match_count": 0}


def verify_layout(layout, digest, source):
    digest_value(digest)
    source_sha(source)
    blobs = layout / "blobs/sha256"
    if any(p.is_symlink() for p in (layout, layout / "blobs", blobs)):
        raise Failure("oci_symlink")
    inventory = {}
    for blob in blobs.iterdir():
        if blob.is_symlink() or not blob.is_file() or not re.fullmatch(r"[a-f0-9]{64}", blob.name):
            raise Failure("unexpected_oci_blob")
        with blob.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != blob.name:
                raise Failure("oci_blob_digest_mismatch")
        inventory[blob.name] = blob.stat().st_size

    def document(identifier):
        digest_value(identifier)
        key = identifier.removeprefix("sha256:")
        if key not in inventory:
            raise Failure("oci_descriptor_missing")
        value = json.loads((blobs / key).read_text())
        if not isinstance(value, dict):
            raise Failure("oci_document_not_object")
        return value

    def descriptor_digest(descriptor):
        if not isinstance(descriptor, dict):
            raise Failure("oci_descriptor_malformed")
        child = digest_value(descriptor.get("digest"))
        if (type(descriptor.get("size")) is not int or
            inventory.get(child.removeprefix("sha256:")) != descriptor["size"] or
            not isinstance(descriptor.get("mediaType"), str)):
            raise Failure("oci_descriptor_missing_or_size_mismatch")
        return child

    entrypoint = layout / "index.json"
    if entrypoint.is_symlink():
        raise Failure("oci_symlink")
    transport = json.loads(entrypoint.read_text())
    if (not isinstance(transport, dict) or transport.get("schemaVersion") != 2 or
        not isinstance(transport.get("manifests"), list) or len(transport["manifests"]) != 1 or
        descriptor_digest(transport["manifests"][0]) != digest):
        raise Failure("oci_transport_target_mismatch")
    index = document(digest)
    if index.get("schemaVersion") != 2 or not isinstance(index.get("manifests"), list):
        raise Failure("oci_index_missing")
    pending, visited = [digest], set()
    while pending:
        identifier = pending.pop()
        if identifier in visited:
            continue
        visited.add(identifier)
        item = document(identifier)
        descriptors = item.get("manifests", []) + item.get("layers", [])
        if "config" in item:
            descriptors.append(item["config"])
        for descriptor in descriptors:
            child = descriptor_digest(descriptor)
            if "manifest" in descriptor["mediaType"] or "index" in descriptor["mediaType"]:
                pending.append(child)
    runtime = [m for m in index["manifests"] if m.get("platform") == {"architecture": "amd64", "os": "linux"}]
    if len(runtime) != 1:
        raise Failure("runtime_platform_ambiguous")
    attestations = [m for m in index["manifests"] if m.get("annotations", {}).get(
        "vnd.docker.reference.type") == "attestation-manifest" and m["annotations"].get(
        "vnd.docker.reference.digest") == runtime[0]["digest"]]
    if not attestations or len(runtime) + len(attestations) != len(index["manifests"]):
        raise Failure("runtime_attestation_mismatch")
    predicates = set()
    for attestation in attestations:
        for layer in document(attestation["digest"]).get("layers", []):
            if layer["mediaType"] != "application/vnd.in-toto+json":
                raise Failure("attestation_payload_type_mismatch")
            statement = document(layer["digest"])
            if not any(subject.get("digest") == {"sha256": runtime[0]["digest"][7:]}
                       for subject in statement.get("subject", [])):
                raise Failure("attestation_subject_mismatch")
            predicates.add(statement.get("predicateType"))
    if ("https://spdx.dev/Document" not in predicates or not predicates.intersection({
        "https://slsa.dev/provenance/v0.2", "https://slsa.dev/provenance/v1"})):
        raise Failure("provenance_or_sbom_missing")
    config = document(document(runtime[0]["digest"])["config"]["digest"])["config"]
    labels = config.get("Labels", {})
    if (labels.get("org.opencontainers.image.revision") != source or
        labels.get("org.opencontainers.image.source") != "https://github.com/" + REPOSITORY or
        config.get("User") != "10001:10001"):
        raise Failure("runtime_source_or_user_mismatch")
    return inventory


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
