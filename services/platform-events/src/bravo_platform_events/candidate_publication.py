"""Read T7 publication evidence before entering T10's plan transaction.

The operator pins the trusted workflow and run/artifact identities. This module
has no dispatch, build, registry-write, state-transition or credential API.
"""

import hashlib
import io
import json
import re
import stat
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from bravo_agent.contracts import digest

from .initial_proposal import (
    CandidatePublication,
    InitialInput,
    InitialIntent,
    InitialProposalExecution,
    InitialResult,
)

REPOSITORY = "Softbank-Hackathon-Finals/demo-chat"
WORKFLOW = ".github/workflows/publish-candidate-image.yml"
IMAGE = "ghcr.io/softbank-hackathon-finals/demo-chat"
MAX_RECEIPT = 128 * 1024
MAX_ARCHIVE = 1024 * 1024


def _require(condition):
    if not condition:
        raise ValueError("candidate_publication_unconfirmed")


def _json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value)
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique)


def publication_request_id(intent: InitialIntent) -> str:
    """Map the immutable 69-character T10 ID to T7's 64-character input."""
    intent = InitialIntent.model_validate_json(intent.model_dump_json())
    return hashlib.sha256(intent.request_id.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class PublicationSelection:
    """Trusted host configuration, never a model or HTTP request field."""

    workflow_sha: str
    workflow_id: int
    run_id: int
    build_attempt: int
    ghcr_attempt: int
    ecr_attempt: int
    build_artifact_id: int
    ghcr_artifact_id: int
    ecr_artifact_id: int
    candidate_sha: str
    pull_request: int

    def __post_init__(self):
        for sha in (self.workflow_sha, self.candidate_sha):
            _require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha))
        for key, value in vars(self).items():
            if not key.endswith("sha"):
                _require(type(value) is int and value > 0)
        _require(self.build_attempt <= self.ghcr_attempt <= self.ecr_attempt)
        _require(len({self.build_artifact_id, self.ghcr_artifact_id, self.ecr_artifact_id}) == 3)


class CandidateGitHub:
    """Read-only GitHub transport using the operator's existing gh login."""

    @staticmethod
    def _get(path, limit):
        _require(path.startswith(f"repos/{REPOSITORY}/actions/"))
        try:
            result = subprocess.run(["gh", "api", "--method", "GET", path],
                                    capture_output=True, timeout=60, check=False)
            _require(result.returncode == 0 and len(result.stdout) <= limit)
            return result.stdout
        except Exception:
            raise ValueError("candidate_provider_unavailable") from None

    def get(self, path):
        return _json(self._get(path, 2 * 1024 * 1024))

    def download(self, artifact_id):
        _require(type(artifact_id) is int and artifact_id > 0)
        return self._get(f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip", MAX_ARCHIVE)


class CandidateGit:
    """Read immutable objects from a host-owned clone; never checkout or execute."""

    def __init__(self, repository):
        self.repository = Path(repository).resolve(strict=True)

    def _git(self, *args):
        result = subprocess.run(["git", "--no-replace-objects", "-C", str(self.repository),
            "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args],
            capture_output=True, timeout=20, check=False)
        _require(result.returncode == 0 and len(result.stdout) <= 2 * 1024 * 1024)
        return result.stdout

    def verify(self, value, result, selection):
        base, head = value.binding.source_sha, selection.candidate_sha
        for sha in (base, head):
            _require(self._git("rev-parse", sha + "^{commit}").decode().strip() == sha)
        self._git("merge-base", "--is-ancestor", base, head)
        edits = {edit.path: edit for edit in result.proposal.edits}
        names = self._git("diff", "--name-only", "-z", "--no-renames", base, head, "--")
        _require(set(filter(None, names.decode().split("\0"))) == set(edits))
        for name, edit in edits.items():
            # A replacement cannot add/delete/rename a file, change its mode, or
            # introduce symlinks/submodules absent from the saved Proposal.
            entries = []
            for sha in (base, head):
                row = self._git("ls-tree", "-z", sha, "--", name).split(b"\0")
                _require(len(row) == 2 and not row[1])
                meta, path = row[0].split(b"\t", 1)
                mode, kind, oid = meta.decode().split()
                _require(path.decode() == name and kind == "blob" and mode in {"100644", "100755"})
                entries.append((mode, self._git("cat-file", "blob", oid)))
            _require(entries[0][0] == entries[1][0])
            _require(entries[0][1] == value.files[name].encode("utf-8"))
            _require(hashlib.sha256(entries[0][1]).hexdigest() == edit.before_sha256)
            _require(entries[1][1] == edit.replacement.encode("utf-8"))
        raw = self._git("diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
                        "--no-renames", base, head, "--")
        return hashlib.sha256(raw).hexdigest()


def _run(github, selected, attempt, required_jobs, *, complete=False):
    prefix = f"repos/{REPOSITORY}/actions/runs/{selected.run_id}/attempts/{attempt}"
    run = github.get(prefix)
    _require(run.get("id") == selected.run_id and type(run.get("id")) is int)
    _require(run.get("run_attempt") == attempt and type(run.get("run_attempt")) is int)
    _require(type(run.get("workflow_id")) is int and run["workflow_id"] == selected.workflow_id)
    _require(run.get("repository", {}).get("full_name") == REPOSITORY)
    _require(run.get("head_repository", {}).get("full_name") == REPOSITORY)
    _require(run.get("head_sha") == selected.workflow_sha and run.get("head_branch") == "main")
    _require(run.get("path") == WORKFLOW and run.get("event") == "workflow_dispatch")
    _require(run.get("status") == "completed")
    if complete:
        _require(run.get("conclusion") == "success")
    data = github.get(prefix + "/jobs?per_page=100")
    jobs = data.get("jobs", [])
    _require(type(data.get("total_count")) is int and data["total_count"] == len(jobs))
    for job in required_jobs:
        matches = [v for v in jobs if v.get("name") == job]
        _require(len(matches) == 1)
        found = matches[0]
        _require(type(found.get("run_id")) is int and found["run_id"] == selected.run_id)
        _require(type(found.get("run_attempt")) is int and found["run_attempt"] == attempt)
        _require(found.get("head_sha") == selected.workflow_sha)
        _require(found.get("status") == "completed" and found.get("conclusion") == "success")


def _artifact(github, selected, identity, name, *, filename=None):
    value = github.get(f"repos/{REPOSITORY}/actions/artifacts/{identity}")
    _require(type(value.get("id")) is int and value["id"] == identity)
    _require(value.get("name") == name and value.get("expired") is False)
    run = value.get("workflow_run", {})
    _require(type(run.get("id")) is int and run["id"] == selected.run_id)
    _require(run.get("head_sha") == selected.workflow_sha)
    fingerprint = value.get("digest", "")
    _require(isinstance(fingerprint, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", fingerprint))
    if filename is None:
        return fingerprint
    _require(type(value.get("size_in_bytes")) is int and 0 < value["size_in_bytes"] <= MAX_ARCHIVE)
    raw = github.download(identity)
    _require(len(raw) <= MAX_ARCHIVE and "sha256:" + hashlib.sha256(raw).hexdigest() == fingerprint)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        _require(len(entries) == 1 and entries[0].filename == filename)
        info = entries[0]
        mode = info.external_attr >> 16
        _require(not info.is_dir() and not stat.S_ISLNK(mode) and not info.flag_bits & 1)
        _require(0 < info.file_size <= MAX_RECEIPT)
        receipt = _json(archive.read(info))
    _require(isinstance(receipt, dict))
    return receipt, fingerprint


def _receipt(receipt, selected, value, intent, diff_hash, destination, attempt):
    expected = dict(schema_version=1, repository=REPOSITORY, base_sha=value.binding.source_sha,
        source_sha=selected.candidate_sha, workflow_sha=selected.workflow_sha,
        pull_request=selected.pull_request, diff_sha256=diff_hash,
        request_id=publication_request_id(intent), workflow_run_id=str(selected.run_id),
        workflow_run_attempt=str(selected.build_attempt), image=IMAGE,
        build_artifact_id=str(selected.build_artifact_id), publisher_run_attempt=str(attempt),
        destination=destination, tag=f"candidate-{selected.run_id}-{selected.build_attempt}",
        scanned_before_publish=True, vulnerability_count=0, secret_match_count=0,
        runtime_packaging_probe_verified=False, runtime_probe_evidence="candidate_process_self_report",
        runtime_probe_self_report="pass", runtime_probe_in_build_job_only=True,
        candidate_executed_on_runner=False, all_blobs_identical=True,
        candidate_executed_in_publisher=False, rebuilt=False,
        t15_rehearsal_verified=False, t31_deployment_verified=False)
    for key, expected_value in expected.items():
        _require(type(receipt.get(key)) is type(expected_value) and receipt[key] == expected_value)
    _require(type(receipt.get("verified_blob_count")) is int and receipt["verified_blob_count"] > 0)
    _require(isinstance(receipt.get("digest"), str) and re.fullmatch(r"sha256:[a-f0-9]{64}", receipt["digest"]))


def prepare_publication(*, value: InitialInput, intent: InitialIntent, result: InitialResult,
                        selection: PublicationSelection, repository, github=None):
    """Authenticate an existing T7 run. Returned reader performs only local checks.

    The configured workflow_sha must be an independently reviewed T7 publisher
    revision. No receipt-supplied workflow revision is accepted as a trust root.
    """
    value = InitialInput.model_validate_json(value.model_dump_json())
    intent = InitialIntent.model_validate_json(intent.model_dump_json())
    _require((intent.plan_id, intent.plan_version, intent.input_digest, intent.binding_digest) == (
        value.binding.plan_id, value.binding.plan_version, digest(value), digest(value.binding)))
    result = InitialProposalExecution._checked(intent, value, result)
    github = github or CandidateGitHub()
    diff_hash = CandidateGit(repository).verify(value, result, selection)
    runs = {}
    for attempt, job in ((selection.build_attempt, "build"), (selection.ghcr_attempt, "ghcr"),
                         (selection.ecr_attempt, "ecr")):
        runs.setdefault(attempt, set()).add(job)
    for attempt, jobs in runs.items():
        _run(github, selection, attempt, jobs, complete=attempt == selection.ecr_attempt)
    build_hash = _artifact(github, selection, selection.build_artifact_id,
                          f"candidate-bundle-{selection.run_id}-{selection.build_attempt}")
    ghcr, ghcr_hash = _artifact(github, selection, selection.ghcr_artifact_id,
        f"candidate-ghcr-receipt-{selection.run_id}-{selection.ghcr_attempt}",
        filename="ghcr-candidate-receipt.json")
    ecr, ecr_hash = _artifact(github, selection, selection.ecr_artifact_id,
        f"candidate-ecr-receipt-{selection.run_id}-{selection.ecr_attempt}",
        filename="ecr-candidate-receipt.json")
    _receipt(ghcr, selection, value, intent, diff_hash, "ghcr-private", selection.ghcr_attempt)
    _receipt(ecr, selection, value, intent, diff_hash, "aws-ecr-private", selection.ecr_attempt)
    for key in set(ghcr) | set(ecr):
        if key not in {"destination", "publisher_run_attempt"}:
            _require(ghcr.get(key) == ecr.get(key))
    bound = digest(dict(input=value.model_dump(), intent=intent.model_dump(), result=result.model_dump()))
    record = CandidatePublication(receipt_id="t7-" + digest(dict(selection=vars(selection),
        artifacts=[build_hash, ghcr_hash, ecr_hash], bound=bound)), request_id=intent.request_id,
        plan_digest=value.binding.plan_digest, source_sha=value.binding.source_sha,
        proposal_digest=result.proposal_digest, diff_hash="sha256:" + diff_hash, image_digest=ecr["digest"])
    frozen = record.model_dump_json()

    def read(_connection, current_value, current_intent, current_result):
        _require(digest(dict(input=current_value.model_dump(), intent=current_intent.model_dump(),
                            result=current_result.model_dump())) == bound)
        return CandidatePublication.model_validate_json(frozen)

    return read
