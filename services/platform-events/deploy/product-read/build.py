"""Build an amd64 reader from committed source, without credentials or deployment."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
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
    "inventory": "bravo_inventory",
    "observability": "bravo_observability",
}

# Include the lazy imports used by --initial-proposal, not just the entry point.
READER_IMPORT_PROBE = (
    "import sys; from bravo_platform_events.product_read_server import load_app, load_initial_app; "
    "from bravo_platform_events.initial_proposal_api import AuthenticatedInitialReader, create_initial_read_app; "
    "from bravo_platform_events.initial_stored_source import stored_initial_source; "
    "from bravo_platform_events.product_access import ProductInputAccess; "
    "from bravo_agent.product_contracts import ProductInput; "
    "from rehearsal_runner.task_catalog import TaskCatalog; "
    "forbidden = [n for n in sys.modules if n.startswith(('openai', "
    "'bravo_agent.subscription_product', 'bravo_agent.subscription_admin', "
    "'bravo_agent.product_execution_mailbox', 'bravo_platform_events.initial_proposal_transport'))]; "
    "assert not forbidden, forbidden; "
    "print('product_reader_imports_ok')"
)


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


def add_rehearsal_context(context, revision):
    copy_committed_tree(revision, "services/rehearsal-runner/templates/", context / "templates")
    for directory in ("rehearsal-access", "target-secrets"):
        copy_committed_tree(revision, f"infra/envs/{directory}/", context / "infra" / directory)
    # Keep the upstream command unchanged; compile its locked, patched module
    # graph with a fixed toolchain instead of shipping vulnerable release deps.
    with tempfile.TemporaryDirectory(prefix="bravo-kubectl-") as directory:
        source = Path(directory)
        copy_committed_tree(revision, "operations/kubectl/", source)
        env = os.environ | {"GOOS": "linux", "GOARCH": "amd64", "CGO_ENABLED": "0",
                            "GOTOOLCHAIN": "go1.26.10"}
        flags = "-s -w -buildid= " + " ".join(
            f"-X {package}.gitVersion=v1.35.10-bravo.1 -X {package}.gitTreeState=modified"
            for package in ("k8s.io/component-base/version", "k8s.io/client-go/pkg/version"))
        subprocess.run(["go", "build", "-mod=readonly", "-trimpath", "-buildvcs=false",
                        "-ldflags", flags, "-o", str(context / "kubectl"), "."],
                       cwd=source, env=env, check=True)


def prepare_context(context, revision, *, rehearsal_host=False):
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
        "--no-emit-package", "bravo-inventory",
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
    if rehearsal_host:
        add_rehearsal_context(context, revision)
    recipe = ("services/platform-events/deploy/approval/Dockerfile" if rehearsal_host
              else "services/platform-events/deploy/product-read/Dockerfile")
    (context / "Dockerfile").write_bytes(git("show", f"{revision}:{recipe}"))
    return [{"path": str(p.relative_to(context)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(context.rglob("*")) if p.is_file()]


def archive_identity(archive, descriptor, revision):
    """Read bounded OCI metadata without extracting files or confusing config/index IDs."""
    with tarfile.open(archive) as stream:
        names = stream.getnames()
        if len(names) != len(set(names)):
            raise ValueError("duplicate_archive_member")

        def read(name):
            member = stream.getmember(name)
            if not member.isfile() or member.size > 2 * 1024 * 1024:
                raise ValueError("invalid_image_metadata")
            return stream.extractfile(member).read()

        def blob(item):
            digest = item.get("digest", "")
            if re.fullmatch(r"sha256:[a-f0-9]{64}", digest) is None:
                raise ValueError("invalid_image_digest")
            data = read("blobs/sha256/" + digest[7:])
            if len(data) != item.get("size") or hashlib.sha256(data).hexdigest() != digest[7:]:
                raise ValueError("image_metadata_digest_mismatch")
            return json.loads(data)

        roots = json.loads(read("index.json"))["manifests"]
        if len(roots) != 1 or roots[0]["digest"] != descriptor["digest"]:
            raise ValueError("image_archive_identity_mismatch")
        document = blob(roots[0])
        manifest = roots[0]
        if "manifests" in document:
            candidates = [m for m in document["manifests"]
                          if m.get("platform") == {"architecture": "amd64", "os": "linux"}]
            if len(candidates) != 1:
                raise ValueError("single_amd64_image_required")
            manifest = candidates[0]
            document = blob(manifest)
        config = blob(document["config"])
        if ((config.get("architecture"), config.get("os"), config["config"].get("User"))
                != ("amd64", "linux", "10001:10001")
                or config["config"].get("Labels", {}).get("org.opencontainers.image.revision") != revision):
            raise ValueError("image_configuration_mismatch")
        return {"image_descriptor": descriptor, "platform_manifest_digest": manifest["digest"],
                "config_digest": document["config"]["digest"], "uid_gid": "10001:10001"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-cache", action="store_true", help="Rebuild only this image if local build cache is unusable")
    parser.add_argument("--rehearsal-host", action="store_true", help="Build the stored-proposal T15 host")
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
        hashes = prepare_context(context, revision, rehearsal_host=args.rehearsal_host)
        context_digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        tag = ("bravo-rehearsal-host:" if args.rehearsal_host else "bravo-product-read:") + revision[:12] + "-" + context_digest[:32]
        subprocess.run(["docker", "build", "--platform", "linux/amd64", "--tag", tag,
            "--label", "org.opencontainers.image.revision=" + revision,
            *(["--no-cache"] if args.no_cache else []), str(context)], check=True)
        image = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]
        probe = "import os; assert os.getuid()==10001; " + READER_IMPORT_PROBE
        if args.rehearsal_host:
            probe = ("import os,sys,subprocess; from bravo_platform_events.credential_host import create_ports; "
                     "from bravo_platform_events.rehearsal_server import load_host; "
                     "assert os.getuid()==10001; "
                     "assert not any(n.startswith(('openai','bravo_agent.subscription_product',"
                     "'bravo_agent.subscription_admin')) for n in sys.modules); "
                     "from rehearsal_runner.namespace import render; "
                     "profile={n+'_'+k:v for n in ('OPENBAO','SEAWEEDFS') for k,v in "
                     "(('NAMESPACE','fixture'),('LABEL_KEY','app'),('LABEL_VALUE','fixture'),('PORT','8200'))}; "
                     "assert 'ResourceQuota' in render('rehearsal-build',profile); "
                     "subprocess.run(['kubectl','version','--client','-o','json'],check=True); "
                     "print('rehearsal_host_imports_ok')")
        subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            *(["--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777"] if args.rehearsal_host else []),
            "--entrypoint", "python", image["Id"], "-c", probe], check=True)
        if (image["Architecture"], image["Os"], image["Config"]["User"]) != ("amd64", "linux", "10001:10001"):
            raise ValueError("product_image_identity_mismatch")
        output.mkdir(parents=True)
        archive = output / "image.tar"
        subprocess.run(["docker", "save", "--output", str(archive), tag], check=True)
        identity = archive_identity(archive, image["Descriptor"], revision)
        with archive.open("rb") as stream:
            archive_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        receipt = dict(source_revision=revision, image=tag, image_id=image["Id"], platform="linux/amd64",
            archive_sha256=archive_digest, context_sha256=context_digest, source_sha256=hashes,
            observability_revision=OBSERVABILITY_REVISION, runtime_imports="pass",
            deployed=False, registry_push=False, **identity)
        (output / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps({k: receipt[k] for k in ("source_revision", "image_id", "archive_sha256")}))


if __name__ == "__main__":
    main()
