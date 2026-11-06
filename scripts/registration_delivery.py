"""Publish reviewed platform service bytes and propose their GitOps digest, never deploy."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
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
PUBLICATION = str(Path(VALUES).with_name("registration-publication.json"))
PROPOSAL_PREFIX = "feature/T43-registration-image-"
PROPOSAL_TITLE = "[T43] 검증된 등록 API 이미지 배포"
BUILD_INPUTS = (
    "services/platform-events",
    "services/agent-worker/src",
    "services/agent-worker/pyproject.toml",
    "services/rehearsal-runner/src",
    "services/rehearsal-runner/pyproject.toml",
    "services/inventory/src",
    "services/inventory/pyproject.toml",
    "services/control-gate/src",
    "services/control-gate/pyproject.toml",
)
INPUTS = (
    *BUILD_INPUTS,
    "scripts/registration_delivery.py",
    "scripts/image_delivery",
    ".github/workflows/publish-registration-image.yml",
)


@dataclass(frozen=True)
class Service:
    slug: str
    title: str

    @property
    def image(self):
        return "ghcr.io/softbank-hackathon-finals/platform-" + self.slug

    @property
    def package(self):
        return "softbank-hackathon-finals/platform-" + self.slug

    @property
    def values(self):
        return "infra/gitops/environments/development/" + self.slug + "-image.json"

    @property
    def publication(self):
        return self.slug + "-publication.json"

    @property
    def inputs(self):
        return (*BUILD_INPUTS, "scripts/registration_delivery.py", "scripts/image_delivery",
                ".github/workflows/publish-" + self.slug + "-image.yml")


REGISTRATION = Service("registration", "등록 API")
PRODUCT_READER = Service("product-reader", "Product Reader")
SERVICES = {item.slug: item for item in (REGISTRATION, PRODUCT_READER)}


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


def verify(root, revision, digest, service=REGISTRATION):
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
        "image": service.image,
        "image_digest": digest,
        "platform_manifest_digest": platform["digest"],
        "config_digest": manifest["config"]["digest"],
        "context_sha256": context["context_sha256"],
        "oci_blobs": len(blobs),
        "scan": scan_summary(report),
    }


def package_visibility(service=REGISTRATION):
    request = urllib.request.Request(
        "https://api.github.com/orgs/Softbank-Hackathon-Finals/packages/container/platform-" + service.slug,
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


def remote_digest(tag, service=REGISTRATION):
    basic = base64.b64encode(
        (os.environ["GITHUB_ACTOR"] + ":" + os.environ["GH_TOKEN"]).encode()
    ).decode()
    request = urllib.request.Request(
        "https://ghcr.io/token?service=ghcr.io&scope=repository:" + service.package + ":pull",
        headers={"Authorization": "Basic " + basic},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.load(response)["token"]
        request = urllib.request.Request(
            "https://ghcr.io/v2/" + service.package + "/manifests/" + tag,
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


def publish(root, receipt, service=REGISTRATION):
    workflow_boundary()
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    if not (
        run_id.isdigit() and attempt.isdigit() and int(run_id) > 0 and int(attempt) > 0
    ):
        raise Failure("publication_run_identity_missing")
    tag = "sha-" + receipt["source_revision"] + "-" + run_id + "-" + attempt
    visibility = package_visibility(service)
    if visibility not in (None, "private"):
        raise Failure("private_package_required")
    existing = remote_digest(tag, service) if visibility else None
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
                "docker://" + service.image + ":" + tag,
            ],
            check=False,
        )
        # A lost copy response is resolved by the same tag's actual manifest.
        if result.returncode and remote_digest(tag, service) != receipt["image_digest"]:
            raise Failure("publication_unknown_read_existing_tag_before_retry")
    if (
        package_visibility(service) != "private"
        or remote_digest(tag, service) != receipt["image_digest"]
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


def promotion_values(receipt, service=REGISTRATION):
    if (
        receipt.get("published") is not True
        or receipt.get("registry_visibility") != "private"
        or receipt.get("image") != service.image
        or receipt.get("deployed") is not False
    ):
        raise Failure("verified_private_publication_required")
    return {
        "image": {"repository": service.image, "digest": digest_value(receipt["image_digest"])},
        "sourceRevision": source_sha(receipt["source_revision"]),
    }


def github(path, *args, check=True):
    return run(["gh", "api", "repos/" + REPOSITORY + "/" + path, *args], check=check)


def open_proposals():
    pages = json.loads(github("pulls?state=open&base=develop&per_page=100",
                              "--paginate", "--slurp").stdout)
    return [pr for page in pages for pr in page]


def proposal_values(pr, service=REGISTRATION):
    """Recognize both legacy and new generated PRs by scope and committed receipt."""
    branch = pr["head"]["ref"]
    if (pr["base"]["ref"] != "develop"
            or (pr["head"].get("repo") or {}).get("full_name") != REPOSITORY
            or not re.fullmatch(re.escape("feature/T43-" + service.slug + "-image-") + r"[0-9a-f]{12}", branch)
            or pr["title"] != "[T43] 검증된 " + service.title + " 이미지 배포"
            or not (pr.get("body") or "").startswith("Refs #355\n")):
        return None
    pages = json.loads(github(f"pulls/{pr['number']}/files?per_page=100",
                              "--paginate", "--slurp").stdout)
    files = [item for page in pages for item in page]
    if ({item["filename"] for item in files} != {service.values, str(Path(service.values).with_name(service.publication))}
            or any(item["status"] not in {"added", "modified"} for item in files)):
        return None
    head = source_sha(pr["head"]["sha"])

    def content(path):
        blob = json.loads(github(f"contents/{path}?ref={head}").stdout)
        return json.loads(base64.b64decode(blob["content"]))

    values = content(service.values)
    if (promotion_values(content(str(Path(service.values).with_name(service.publication))), service) != values
            or branch != "feature/T43-" + service.slug + "-image-" + values["sourceRevision"][:12]):
        raise Failure("proposal_identity_mismatch")
    return values


def is_ancestor(older, newer):
    result = run(["git", "merge-base", "--is-ancestor", older, newer], check=False)
    if result.returncode not in {0, 1}:
        raise Failure("proposal_source_history_unavailable")
    return result.returncode == 0


def close_superseded(values, keep=None, service=REGISTRATION):
    """Close only older generated proposals after confirming their replacement."""
    source = values["sourceRevision"]
    run(["git", "fetch", "origin", "develop"])
    if not is_ancestor(source, "origin/develop") or run(
        ["git", "diff", "--name-only", source, "origin/develop", "--", *service.inputs]
    ).stdout.strip():
        return []
    if keep is not None:
        replacement = json.loads(github(f"pulls/{keep}").stdout)
        if replacement["state"] != "open" or proposal_values(replacement, service) != values:
            raise Failure("replacement_proposal_changed")
    else:
        recorded = json.loads(run(["git", "show", "origin/develop:" + service.values]).stdout)
        if recorded != values:
            raise Failure("replacement_values_changed")
    closed = []
    for pr in open_proposals():
        if pr["number"] == keep:
            continue
        previous = proposal_values(pr, service)
        if previous is None or previous["sourceRevision"] == source:
            continue
        if not is_ancestor(previous["sourceRevision"], source):
            continue
        current = json.loads(github(f"pulls/{pr['number']}").stdout)
        if current["state"] != "open":
            continue
        if current["head"]["sha"] != pr["head"]["sha"] or proposal_values(current, service) != previous:
            raise Failure("superseded_proposal_changed")
        github(f"pulls/{pr['number']}", "--method", "PATCH", "-f", "state=closed", check=False)
        # Resolve a lost close response by reading the same PR, never retrying the write.
        readback = json.loads(github(f"pulls/{pr['number']}").stdout)
        if readback["state"] != "closed" or readback.get("merged_at") is not None:
            raise Failure("proposal_close_unknown_read_existing_pr_before_retry")
        closed.append(pr["number"])
    return closed


def propose(receipt, service=REGISTRATION):
    workflow_boundary()
    values = promotion_values(receipt, service)
    source = values["sourceRevision"]
    run(["git", "fetch", "origin", "develop"])
    run(["git", "merge-base", "--is-ancestor", source, "origin/develop"])
    # Build completion ordering cannot regress newer service code.
    if run(
        ["git", "diff", "--name-only", source, "origin/develop", "--", *service.inputs]
    ).stdout.strip():
        return {"action": "superseded", "deployed": False}
    # The workflow's shared concurrency group serializes publication/proposal runs.
    # Documentation-only newer sources can have identical inputs: do not regress them.
    for pr in open_proposals():
        pending = proposal_values(pr, service)
        if (pending is not None and pending["sourceRevision"] != source
                and is_ancestor(source, pending["sourceRevision"])):
            return {"action": "superseded", "deployed": False}
    branch = "feature/T43-" + service.slug + "-image-" + source[:12]
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
        closed = []
        if existing[0]["state"] == "OPEN":
            pr = json.loads(github(f"pulls/{existing[0]['number']}").stdout)
            pending = proposal_values(pr, service)
            if pending is None or pending["sourceRevision"] != source:
                raise Failure("existing_proposal_identity_mismatch")
            closed = close_superseded(pending, pr["number"], service)
        return {
            "action": "existing_proposal",
            "url": existing[0]["url"],
            "closed_proposals": closed,
            "deployed": False,
        }
    run(["git", "switch", "--detach", "origin/develop"])
    path = Path(service.values)
    if path.exists() and json.loads(path.read_text()) == values:
        return {"action": "already_recorded", "closed_proposals": close_superseded(values, service=service),
                "deployed": False}
    previous = run(["git", "show", source + ":" + service.values], check=False)
    previous_values = json.loads(previous.stdout) if previous.returncode == 0 else None
    current_values = json.loads(path.read_text()) if path.exists() else None
    if current_values != previous_values:
        return {"action": "deployment_values_changed", "deployed": False}
    run(["git", "switch", "-c", branch])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, indent=2) + "\n")
    Path(service.values).with_name(service.publication).write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    run(["git", "add", service.values, str(path.with_name(service.publication))])
    run(
        [
            "git",
            "commit",
            "-m",
            "[T43] 검증된 " + service.title + " 이미지를 GitOps 개발 환경에 반영",
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
        "Refs #355\n\n검사한 " + service.title + " 이미지를 개발 환경의 Helm 값에 결속합니다. "
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
    body_file = Path(os.environ["RUNNER_TEMP"]) / (service.slug + "-proposal.md")
    body_file.write_text(body)
    run(
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
            "[T43] 검증된 " + service.title + " 이미지 배포",
            "--body-file",
            str(body_file),
            "--label",
            "P0",
            "--label",
            "area:infra",
            "--label",
            "stage:3",
        ], check=False,
    )
    # Successful and lost create responses use the same exact branch readback.
    created = [pr for pr in open_proposals() if pr["head"]["ref"] == branch]
    if (len(created) != 1 or created[0]["head"]["sha"] != head
            or proposal_values(created[0], service) != values):
        raise Failure("proposal_create_unknown_read_existing_pr_before_retry")
    closed = close_superseded(values, created[0]["number"], service)
    return {"action": "proposed", "url": created[0]["html_url"],
            "closed_proposals": closed, "deployed": False}


def check_promotion(service=REGISTRATION):
    # A queue base can be another unmerged queue entry. Compare against the
    # common develop ancestor so every promotion in the combined group is checked.
    base = run(["git", "merge-base", "HEAD", "refs/remotes/origin/develop"]).stdout.strip()
    publication = str(Path(service.values).with_name(service.publication))
    changed = run(["git", "diff", "--name-only", base, "HEAD", "--", service.values, publication])
    if not changed.stdout.strip():
        return {"action": "no_promotion", "deployed": False}
    values = json.loads(run(["git", "show", "HEAD:" + service.values]).stdout)
    receipt = json.loads(run(["git", "show", "HEAD:" + publication]).stdout)
    if promotion_values(receipt, service) != values:
        raise Failure("publication_values_mismatch")
    source = values["sourceRevision"]
    run(["git", "merge-base", "--is-ancestor", source, "HEAD"])
    if run(["git", "diff", "--name-only", source, "HEAD", "--", *BUILD_INPUTS]).stdout.strip():
        raise Failure("promotion_source_superseded")
    return {"action": "current_source_verified", "source_revision": source, "deployed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "publish", "propose", "check-promotion"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--service", choices=tuple(SERVICES), default="registration")
    args = parser.parse_args()
    service = SERVICES[args.service]
    if args.action != "check-promotion" and args.root is None:
        parser.error("--root is required for verify, publish and propose")
    try:
        if args.action == "check-promotion":
            result = check_promotion(service)
        elif args.action == "propose":
            result = propose(
                json.loads((args.root / service.publication).read_text()), service
            )
        else:
            result = verify(
                args.root, os.environ["SOURCE_SHA"], os.environ["BUILD_DIGEST"], service
            )
            if args.action == "publish":
                result = publish(args.root, result, service)
                (args.root / service.publication).write_text(
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
