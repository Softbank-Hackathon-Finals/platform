"""Publish reviewed registration bytes and propose their GitOps digest, never deploy."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from image_delivery.oci import (
    Failure,
    digest_value,
    scan_summary,
    source_sha,
    verify_layout,
)

REPOSITORY = "Softbank-Hackathon-Finals/platform"
IMAGE = "ghcr.io/softbank-hackathon-finals/platform-registration"
PACKAGE = "softbank-hackathon-finals/platform-registration"
VALUES = "infra/gitops/environments/development/registration-image.json"
INPUTS = (
    "services/platform-events",
    "services/agent-worker/src",
    "services/agent-worker/pyproject.toml",
    "services/rehearsal-runner/src",
    "services/rehearsal-runner/pyproject.toml",
    "services/inventory/src",
    "services/inventory/pyproject.toml",
    "scripts/registration_delivery.py",
    "scripts/image_delivery",
    ".github/workflows/publish-registration-image.yml",
)


def run(args, *, check=True):
    result = subprocess.run(
        args, capture_output=True, text=True, check=False, timeout=600
    )
    if check and result.returncode:
        raise Failure("delivery_command_failed")
    return result


def workflow_boundary():
    if (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_REF") != "refs/heads/develop"
        or os.environ.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch"}
    ):
        raise Failure("trusted_develop_workflow_required")


def verify(root, revision, digest):
    source_sha(revision)
    digest_value(digest)
    context = json.loads((root / "source-manifest.json").read_text())
    if context.get("source_revision") != revision:
        raise Failure("source_context_mismatch")
    layout = root / "image-layout"
    blobs = verify_layout(layout, digest, revision, repository=REPOSITORY)
    index = json.loads((layout / "blobs/sha256" / digest[7:]).read_text())
    platform = next(
        m
        for m in index["manifests"]
        if m.get("platform") == {"architecture": "amd64", "os": "linux"}
    )
    manifest = json.loads(
        (layout / "blobs/sha256" / platform["digest"][7:]).read_text()
    )
    report = json.loads((root / "image-scan.json").read_text())
    if (
        report.get("ArtifactType") != "container_image"
        or report.get("Metadata", {}).get("ImageID") != manifest["config"]["digest"]
    ):
        raise Failure("scan_image_identity_mismatch")
    return {
        "source_revision": revision,
        "image": IMAGE,
        "image_digest": digest,
        "platform_manifest_digest": platform["digest"],
        "config_digest": manifest["config"]["digest"],
        "context_sha256": context["context_sha256"],
        "oci_blobs": len(blobs),
        "scan": scan_summary(report),
    }


def package_visibility():
    request = urllib.request.Request(
        "https://api.github.com/orgs/Softbank-Hackathon-Finals/packages/container/platform-registration",
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)["visibility"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise Failure("package_visibility_unavailable") from None


def remote_digest(tag):
    basic = base64.b64encode(
        (os.environ["GITHUB_ACTOR"] + ":" + os.environ["GH_TOKEN"]).encode()
    ).decode()
    request = urllib.request.Request(
        "https://ghcr.io/token?service=ghcr.io&scope=repository:" + PACKAGE + ":pull",
        headers={"Authorization": "Basic " + basic},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.load(response)["token"]
        request = urllib.request.Request(
            "https://ghcr.io/v2/" + PACKAGE + "/manifests/" + tag,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.oci.image.index.v1+json",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return "sha256:" + hashlib.sha256(response.read()).hexdigest()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise Failure("registry_readback_unavailable") from None


def publish(root, receipt):
    workflow_boundary()
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    if not (
        run_id.isdigit() and attempt.isdigit() and int(run_id) > 0 and int(attempt) > 0
    ):
        raise Failure("publication_run_identity_missing")
    tag = "sha-" + receipt["source_revision"] + "-" + run_id + "-" + attempt
    visibility = package_visibility()
    if visibility not in (None, "private"):
        raise Failure("private_package_required")
    existing = remote_digest(tag) if visibility else None
    if existing is not None and existing != receipt["image_digest"]:
        raise Failure("immutable_tag_conflict")
    if existing is None:
        # Native registry tooling reads its ephemeral Actions login; no token in argv.
        result = run(
            [
                "skopeo",
                "copy",
                "--all",
                "--preserve-digests",
                "oci:" + str(root / "image-layout"),
                "docker://" + IMAGE + ":" + tag,
            ],
            check=False,
        )
        # A lost copy response is resolved by the same tag's actual manifest.
        if result.returncode and remote_digest(tag) != receipt["image_digest"]:
            raise Failure("publication_unknown_read_existing_tag_before_retry")
    if (
        package_visibility() != "private"
        or remote_digest(tag) != receipt["image_digest"]
    ):
        raise Failure("publication_readback_mismatch")
    return receipt | {
        "registry_visibility": "private",
        "tag": tag,
        "run_id": run_id,
        "attempt": attempt,
        "workflow_revision": source_sha(os.environ["GITHUB_WORKFLOW_SHA"]),
        "published": True,
        "deployed": False,
    }


def promotion_values(receipt):
    if (
        receipt.get("published") is not True
        or receipt.get("registry_visibility") != "private"
        or receipt.get("image") != IMAGE
        or receipt.get("deployed") is not False
    ):
        raise Failure("verified_private_publication_required")
    return {
        "image": {"repository": IMAGE, "digest": digest_value(receipt["image_digest"])},
        "sourceRevision": source_sha(receipt["source_revision"]),
    }


def propose(receipt):
    workflow_boundary()
    values = promotion_values(receipt)
    source = values["sourceRevision"]
    run(["git", "fetch", "origin", "develop"])
    run(["git", "merge-base", "--is-ancestor", source, "origin/develop"])
    # Build completion ordering cannot regress newer service code.
    if run(
        ["git", "diff", "--name-only", source, "origin/develop", "--", *INPUTS]
    ).stdout.strip():
        return {"action": "superseded", "deployed": False}
    branch = "feature/T43-registration-image-" + source[:12]
    existing = json.loads(
        run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                REPOSITORY,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,url,state",
            ]
        ).stdout
    )
    if existing:
        return {
            "action": "existing_proposal",
            "url": existing[0]["url"],
            "deployed": False,
        }
    run(["git", "switch", "--detach", "origin/develop"])
    path = Path(VALUES)
    if path.exists() and json.loads(path.read_text()) == values:
        return {"action": "already_recorded", "deployed": False}
    previous = run(["git", "show", source + ":" + VALUES], check=False)
    previous_values = json.loads(previous.stdout) if previous.returncode == 0 else None
    current_values = json.loads(path.read_text()) if path.exists() else None
    if current_values != previous_values:
        return {"action": "deployment_values_changed", "deployed": False}
    run(["git", "switch", "-c", branch])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, indent=2) + "\n")
    Path(VALUES).with_name("registration-publication.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    run(["git", "add", VALUES, str(path.with_name("registration-publication.json"))])
    run(
        [
            "git",
            "commit",
            "-m",
            "[T43] 검증된 등록 API 이미지를 GitOps 개발 환경에 반영",
        ]
    )
    # If a previous push response was lost, the remote branch is read before any retry.
    remote = run(
        ["git", "ls-remote", "--heads", "origin", "refs/heads/" + branch]
    ).stdout.strip()
    if remote:
        raise Failure("existing_remote_branch_requires_readback")
    run(["git", "push", "origin", "HEAD:refs/heads/" + branch], check=False)
    head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    remote = run(
        ["git", "ls-remote", "--heads", "origin", "refs/heads/" + branch]
    ).stdout.split()
    if not remote or remote[0] != head:
        raise Failure("push_unknown_read_existing_branch_before_retry")
    body = (
        "Refs #355\n\n검사한 등록 API 이미지를 개발 환경의 Helm 값에 결속합니다. "
        "이 PR 병합 뒤 Argo CD가 동기화하며 실제 API 확인은 별도 영수증으로 남깁니다.\n\n"
        "- Source: `"
        + source
        + "`\n- Image digest: `"
        + receipt["image_digest"]
        + "`\n"
        "- Publication: https://github.com/"
        + REPOSITORY
        + "/actions/runs/"
        + receipt["run_id"]
        + "\n"
        "- GHCR private readback, OCI/provenance/SBOM and image scan passed.\n"
    )
    body_file = Path(os.environ["RUNNER_TEMP"]) / "registration-proposal.md"
    body_file.write_text(body)
    url = run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            REPOSITORY,
            "--base",
            "develop",
            "--head",
            branch,
            "--title",
            "[T43] 검증된 등록 API 이미지 배포",
            "--body-file",
            str(body_file),
            "--label",
            "P0",
            "--label",
            "area:infra",
            "--label",
            "stage:3",
        ]
    ).stdout.strip()
    return {"action": "proposed", "url": url, "deployed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "publish", "propose"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "propose":
            result = propose(
                json.loads((args.root / "registration-publication.json").read_text())
            )
        else:
            result = verify(
                args.root, os.environ["SOURCE_SHA"], os.environ["BUILD_DIGEST"]
            )
            if args.action == "publish":
                result = publish(args.root, result)
                (args.root / "registration-publication.json").write_text(
                    json.dumps(result, indent=2) + "\n"
                )
        print(json.dumps(result))
    except (
        Failure,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ):
        raise SystemExit(
            "registration_delivery_failed; inspect bounded receipts before retry"
        ) from None


if __name__ == "__main__":
    main()
