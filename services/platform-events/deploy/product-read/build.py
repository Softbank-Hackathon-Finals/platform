"""Build an amd64 reader from committed source, without credentials or deployment."""

import argparse
import hashlib
import json
import subprocess
import tempfile
import tomllib
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[2]
ROOT = SERVICE.parents[1]
OBSERVABILITY_REVISION = "020c6c78bdc6133ab0ee15e2a990bb9527b3182f"
PACKAGES = {
    "platform-events": "bravo_platform_events",
    "agent-worker": "bravo_agent",
    "rehearsal-runner": "rehearsal_runner",
    "observability": "bravo_observability",
}


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def copy_committed_tree(revision, prefix, destination):
    """Copy only regular Git blobs; ignored files, links and working edits stay out."""
    entries = git("ls-tree", "-rz", revision, "--", prefix).split(b"\0")
    count = 0
    for entry in filter(None, entries):
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("regular_committed_sources_required")
        relative = Path(raw_path.decode()).relative_to(prefix)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid_source_path")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git("cat-file", "blob", oid))
        count += 1
    if not count:
        raise ValueError("committed_source_missing")


def prepare_context(context, revision):
    # Export only hashed registry dependencies. Repo packages come from Git blobs.
    project = tomllib.loads((SERVICE / "pyproject.toml").read_text())
    upstream = project["tool"]["uv"]["sources"]["bravo-observability"]
    if (upstream.get("rev") != OBSERVABILITY_REVISION
            or upstream.get("git") != "https://github.com/Softbank-Hackathon-Finals/platform.git"
            or upstream.get("subdirectory") != "services/observability"):
        raise ValueError("observability_source_mismatch")
    requirements = context / "requirements.txt"
    subprocess.run(["uv", "export", "--locked", "--no-header", "--no-dev", "--extra", "postgres",
        "--group", "product-inputs", "--no-emit-project", "--no-emit-package", "bravo-observability",
        "--no-emit-package", "bravo-agent-worker", "--no-emit-package", "bravo-rehearsal-runner",
        "-o", str(requirements)], cwd=SERVICE, check=True, stdout=subprocess.DEVNULL)
    text = requirements.read_text()
    if any(line.strip().startswith(("-e ", "git+", "file:", "../", "/")) or " @ " in line
           for line in text.splitlines() if not line.startswith("#")):
        raise ValueError("registry_dependencies_required")
    for service, package in PACKAGES.items():
        source = OBSERVABILITY_REVISION if service == "observability" else revision
        copy_committed_tree(source, f"services/{service}/src/{package}/", context / "src" / package)
    if not (context / "src/bravo_platform_events/product_read_server.py").is_file():
        raise ValueError("product_reader_missing")
    recipe = "services/platform-events/deploy/product-read/Dockerfile"
    (context / "Dockerfile").write_bytes(git("show", f"{revision}:{recipe}"))
    return [{"path": str(p.relative_to(context)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(context.rglob("*")) if p.is_file()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or output.is_relative_to(ROOT):
        raise ValueError("choose_new_output_directory")
    # uv export reads the checkout, so bind all tracked files to the source commit.
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("clean_committed_checkout_required")
    revision = git("rev-parse", "HEAD").decode().strip()
    with tempfile.TemporaryDirectory(prefix="bravo-product-read-") as directory:
        context = Path(directory)
        hashes = prepare_context(context, revision)
        context_digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        tag = "bravo-product-read:source-" + context_digest
        subprocess.run(["docker", "build", "--platform", "linux/amd64", "--tag", tag,
            "--label", "org.opencontainers.image.revision=" + revision, str(context)], check=True)
        probe = ("import os,sys; from bravo_platform_events.product_read_server import load_app; "
            "from bravo_platform_events.product_access import ProductInputAccess; "
            "from bravo_agent.product_contracts import ProductInput; "
            "from rehearsal_runner.task_catalog import TaskCatalog; "
            "assert os.getuid()==10001; "
            "assert not any(n in sys.modules for n in "
            "('openai','bravo_agent.subscription_product','bravo_agent.subscription_admin')); "
            "print('product_reader_imports_ok')")
        subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--entrypoint", "python", tag, "-c", probe], check=True)
        image = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]
        if (image["Architecture"], image["Os"], image["Config"]["User"]) != ("amd64", "linux", "10001:10001"):
            raise ValueError("product_image_identity_mismatch")
        output.mkdir(parents=True)
        archive = output / "image.tar"
        subprocess.run(["docker", "save", "--output", str(archive), tag], check=True)
        with archive.open("rb") as stream:
            archive_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        receipt = dict(source_revision=revision, image=tag, image_id=image["Id"], platform="linux/amd64",
            archive_sha256=archive_digest, context_sha256=context_digest, source_sha256=hashes,
            observability_revision=OBSERVABILITY_REVISION, runtime_imports="pass",
            deployed=False, registry_push=False)
        (output / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps({k: receipt[k] for k in ("source_revision", "image_id", "archive_sha256")}))


if __name__ == "__main__":
    main()
