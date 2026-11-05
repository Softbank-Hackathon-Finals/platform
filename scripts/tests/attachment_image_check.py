"""Run the operator's image path for real, once, and record what it did. Manual; not part of CI.

Uses a throwaway clone whose origin/develop is the current commit, so the repository is not touched.
It builds the real linux/amd64 archive, checks that the previous base image is refused by the scan, and
imports the archive with the guide's ctr arguments on an emulated amd64 k3s node. That node cannot start
Pods because emulation has no seccomp, so the Job itself is measured separately by attachment_job_k3s.py.
No cloud resource is called. The container is named t8-job-* and removed at the end.

    uv run --no-project --python 3.12 python scripts/tests/attachment_image_check.py --out result.json
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import attachment_image

NODE = "t8-job-amd64-node"
K3S_IMAGE = "mirror.gcr.io/rancher/k3s:v1.35.5-k3s1"
SCANNED_OUT_BASE = "sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"


def run(*command, check=True, timeout=1500):
    done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if check and done.returncode:
        raise RuntimeError(f"{' '.join(command[:4])} failed: {done.stderr[-600:]}")
    return done


def git(clone, *arguments):
    return run("git", "-C", str(clone), "-c", "user.name=T8 check", "-c", "user.email=t8-check@example.invalid",
               *arguments).stdout.strip()


def build_in(clone, output):
    done = run(sys.executable, str(clone / "scripts/attachment_image.py"), "--source", git(clone, "rev-parse", "HEAD"),
               "--output-dir", str(output), check=False)
    return done.returncode, json.loads(done.stdout)


def previous_base_is_refused(clone, output):
    """Only the base digest differs from the real build, so a refusal comes from the scan of that base."""
    dockerfile = clone / attachment_image.CONTEXT_DIRECTORY / attachment_image.DOCKERFILE
    dockerfile.write_text(re.sub(r"(FROM \S+@)sha256:[0-9a-f]{64}", rf"\g<1>{SCANNED_OUT_BASE}", dockerfile.read_text()))
    git(clone, "commit", "-q", "-a", "-m", "previous base")
    git(clone, "update-ref", "refs/remotes/origin/develop", "HEAD")
    code, result = build_in(clone, output)
    return {"exit_code": code, "error_code": result.get("error_code"),
            "archive_written": any(output.glob("*.tar"))}


def import_on_amd64_node(archive, receipt):
    def docker_exec(*command):
        return run("docker", "exec", NODE, *command)

    run("docker", "rm", "-f", NODE, check=False)
    run("docker", "pull", "-q", "--platform", "linux/amd64", K3S_IMAGE)
    run("docker", "run", "-d", "--privileged", "--platform", "linux/amd64", "--name", NODE, "--tmpfs", "/run",
        "--tmpfs", "/var/run", K3S_IMAGE, "server", "--disable", "traefik", "--disable", "servicelb",
        "--disable", "metrics-server", "--disable", "local-storage")
    deadline = time.monotonic() + 300
    while run("docker", "exec", NODE, "ctr", "-n", "k8s.io", "version", check=False).returncode:
        if time.monotonic() > deadline:
            raise TimeoutError("containerd did not start")
        time.sleep(3)
    architecture = docker_exec("uname", "-m").stdout.strip()
    run("docker", "cp", str(archive), NODE + ":/tmp/attachment-copy.tar")
    node_hash = docker_exec("sha256sum", "/tmp/attachment-copy.tar").stdout.split()[0]
    docker_exec("ctr", "-n", "k8s.io", *attachment_image.IMPORT_ARGUMENTS, "/tmp/attachment-copy.tar")

    def listing():
        lines = docker_exec("ctr", "-n", "k8s.io", "images", "ls").stdout.splitlines()
        return {line.split()[0]: line.split() for line in lines[1:] if line.startswith(attachment_image.IMAGE_NAME)}

    digest = receipt["digest"]
    pinned = f"{attachment_image.IMAGE_NAME}@{digest}"
    tag = f"{attachment_image.IMAGE_NAME}:sha-{receipt['source_sha']}"
    before = listing()
    if pinned not in before:
        docker_exec("ctr", "-n", "k8s.io", "images", "tag", tag, pinned)
    after = listing()

    def guide_grep_finds(name):
        # The guide's own filter, on real ctr output: it decides whether the operator continues.
        command = f'ctr -n k8s.io images ls | grep "^{name} " | grep -q " {digest} "'
        return run("docker", "exec", NODE, "sh", "-c", command, check=False).returncode == 0

    return {"guide_grep_finds_the_tag": guide_grep_finds(tag), "guide_grep_finds_the_digest_name": guide_grep_finds(pinned),
            "guide_grep_rejects_another_digest": run(
                "docker", "exec", NODE, "sh", "-c",
                f'ctr -n k8s.io images ls | grep "^{tag} " | grep -q " sha256:{"0" * 64} "', check=False).returncode != 0,
            "node_architecture": architecture, "archive_hash_matches_receipt": node_hash == receipt["archive_sha256"],
            "tag_created_by_import": tag in before, "digest_name_created_by_import": pinned in before,
            "digest_name_after_alias": pinned in after,
            "descriptor_digest_matches_receipt": all(after[name][2] == digest for name in (tag, pinned) if name in after),
            "platform_column": after[pinned][-2:] if pinned in after else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    source = run("git", "-C", str(REPO), "rev-parse", "HEAD").stdout.strip()
    started = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix="t8-image-check-") as temporary:
            work = Path(temporary)
            clone = work / "clone"
            run("git", "clone", "-q", "--no-local", str(REPO), str(clone))
            git(clone, "checkout", "-q", source)
            git(clone, "update-ref", "refs/remotes/origin/develop", source)
            code, receipt = build_in(clone, work / "good")
            if code:
                raise RuntimeError(json.dumps(receipt))
            archive = next((work / "good").glob("*.tar"))
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            refused = previous_base_is_refused(clone, work / "refused")
            imported = import_on_amd64_node(archive, receipt)
    finally:
        run("docker", "rm", "-f", NODE, check=False)
    checks = {
        "receipt_hash_matches_archive": actual == receipt["archive_sha256"],
        "receipt_is_for_this_commit": receipt["source_sha"] == source,
        "amd64_probe_passed": receipt["isolated_runtime_probe_verified"] is True,
        "scan_found_nothing": receipt["scan"]["vulnerability_count"] == 0 and receipt["scan"]["secret_match_count"] == 0,
        "previous_base_refused_by_scan": refused["error_code"] == "image_scan_failed" and not refused["archive_written"],
        "node_is_amd64": imported["node_architecture"] == "x86_64",
        "hash_matches_on_the_node": imported["archive_hash_matches_receipt"],
        "digest_name_points_at_the_receipt_digest": imported["digest_name_after_alias"]
        and imported["descriptor_digest_matches_receipt"],
        "guide_filter_works_on_real_ctr_output": imported["guide_grep_finds_the_tag"]
        and imported["guide_grep_finds_the_digest_name"] and imported["guide_grep_rejects_another_digest"],
    }
    args.out.write_text(json.dumps({
        "artifact_kind": "real amd64 image build and import on an emulated amd64 k3s node; no cloud resource was called",
        "checks": checks, "receipt": receipt, "previous_base": refused, "import": imported,
        "seconds": round(time.time() - started)}, indent=2, ensure_ascii=False) + "\n")
    failed = [name for name, passed in checks.items() if not passed]
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
