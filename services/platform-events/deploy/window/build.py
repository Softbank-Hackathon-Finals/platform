"""Build an allowlisted context from the locked local environment, without credentials."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[2]
REVISION = "24b3cf516fdbdaac7dd346718eaac3c42f5e1d63"


def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "image.tar").exists():
        raise ValueError("choose_new_output_directory")
    subprocess.run(["uv", "sync", "--locked", "--extra", "postgres"], cwd=SERVICE, check=True)
    site = next((SERVICE / ".venv/lib").glob("python*/site-packages"))
    provenance = json.loads(next(site.glob("bravo_observability-*.dist-info/direct_url.json")).read_text())
    if provenance.get("vcs_info", {}).get("commit_id") != REVISION:
        raise ValueError("upstream_revision_mismatch")
    with tempfile.TemporaryDirectory(prefix="bravo-window-build-") as directory:
        context = Path(directory)
        subprocess.run(["uv", "export", "--locked", "--no-dev", "--extra", "postgres", "--no-emit-project",
                        "--no-emit-package", "bravo-observability", "-o", str(context / "requirements.txt")],
                       cwd=SERVICE, check=True, stdout=subprocess.DEVNULL)
        shutil.copyfile(Path(__file__).with_name("Dockerfile"), context / "Dockerfile")
        shutil.copytree(SERVICE / "src", context / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(site / "bravo_observability", context / "upstream/bravo_observability",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        hashes = [{"path": str(p.relative_to(context)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                  for p in sorted(context.rglob("*")) if p.is_file()]
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        tag = "bravo-window-intake:source-" + digest
        subprocess.run(["docker", "build", "--platform", "linux/amd64", "--tag", tag, str(context)], check=True)
        subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
                        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                        "--entrypoint", "python", tag, "-c",
                        "from bravo_platform_events.window_server import runtime_connection; "
                        "from bravo_observability.intake import accept; print('runtime_imports_ok')"], check=True)
        subprocess.run(["docker", "save", "--output", str(output / "image.tar"), tag], check=True)
        image = json.loads(subprocess.check_output(["docker", "inspect", tag]))[0]
        receipt = {"image": tag, "image_id": image["Id"], "platform": "linux/amd64",
                   "archive_sha256": hashlib.file_digest((output / "image.tar").open("rb"), "sha256").hexdigest(),
                   "source_sha256": hashes, "upstream_revision": REVISION}
        (output / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps({k: receipt[k] for k in ("image", "image_id", "archive_sha256")}))


if __name__ == "__main__":
    main()
