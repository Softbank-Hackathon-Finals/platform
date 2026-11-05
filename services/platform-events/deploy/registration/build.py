"""Add Git to a verified same-revision base; no credentials enter the context."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("new_output_directory_required")
    if subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"]):
        raise ValueError("clean_committed_checkout_required")
    revision = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    base = json.loads(args.base_receipt.read_text())
    image = json.loads(subprocess.check_output(["docker", "image", "inspect", base["image"]]))[0]
    if (
        base["source_revision"] != revision
        or image["Id"] != base["image_id"]
        or image["Config"]["Labels"]["org.opencontainers.image.revision"] != revision
        or (image["Architecture"], image["Os"], image["Config"]["User"])
        != ("amd64", "linux", "10001:10001")
    ):
        raise ValueError("same_revision_base_required")
    recipe = (HERE / "Dockerfile").read_bytes()
    binding = hashlib.sha256(recipe + base["image_id"].encode()).hexdigest()
    tag = "bravo-registration:" + revision[:12] + "-" + binding[:32]
    with tempfile.TemporaryDirectory(prefix="bravo-registration-build-") as directory:
        (Path(directory) / "Dockerfile").write_bytes(recipe)
        subprocess.run(
            [
                "docker",
                "build",
                "--platform",
                "linux/amd64",
                "--pull=false",
                "--build-arg",
                "BASE_IMAGE=" + base["image"],
                "--tag",
                tag,
                "--label",
                "org.opencontainers.image.revision=" + revision,
                directory,
            ],
            check=True,
        )
    built = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]
    probe = (
        "import os,subprocess; from bravo_platform_events.registration_server import build_app; "
        "from bravo_platform_events.registration_sso import SsoRegistrationVerifier; "
        "assert os.getuid()==10001; "
        "print(subprocess.check_output(['git','--version'],text=True).strip())"
    )
    git_version = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "python",
            built["Id"],
            "-c",
            probe,
        ],
        text=True,
    ).strip()
    args.output.mkdir(parents=True)
    archive = args.output / "image.tar"
    subprocess.run(["docker", "save", "--output", str(archive), tag], check=True)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    receipt = {
        "source_revision": revision,
        "image": tag,
        "image_id": built["Id"],
        "base_image_id": base["image_id"],
        "recipe_sha256": hashlib.sha256(recipe).hexdigest(),
        "base_receipt_sha256": hashlib.sha256(args.base_receipt.read_bytes()).hexdigest(),
        "archive_sha256": digest,
        "platform": "linux/amd64",
        "runtime_user": "10001:10001",
        "runtime_imports": "pass",
        "git_version": git_version,
        "deployed": False,
    }
    (args.output / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps({k: receipt[k] for k in ("image", "image_id", "archive_sha256", "git_version")})
    )


if __name__ == "__main__":
    main()
