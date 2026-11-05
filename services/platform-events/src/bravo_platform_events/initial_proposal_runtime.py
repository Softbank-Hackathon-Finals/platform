"""T38 initial proposal producer: current authority, one journal claim, T11 validation.

Initial inputs never become a rehearsal Admission. The trusted T10 source has
already verified T16's selected item; its v2 analysis is covered by input_digest.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from bravo_agent.contracts import Contract, FailureBundle, digest
from bravo_agent.subscription_product import ProductAccess, proposal_result, stopped
from bravo_agent.subscription_product_reader import MAX_READ_BYTES, ProductReaderConfig
from bravo_agent.tools import Snapshot, Tools
from pydantic import Field

from .initial_proposal import InitialIntent, InitialResult
from .initial_proposal_api import PATH, InitialRead, bound_input


class InitialBrokerRequest(Contract):
    intent: InitialIntent
    github_run_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    web_identity_token: str = Field(min_length=1, max_length=16384, repr=False)


@dataclass(frozen=True)
class InitialInputHTTPReader:
    config: ProductReaderConfig
    token: str = field(repr=False)
    transport: object = field(default=None, repr=False)

    def __call__(self, request):
        try:
            request = InitialRead.model_validate_json(request.model_dump_json())
            if any(c.isspace() for c in self.token) or not 1 <= len(self.token) <= 16384:
                raise ValueError()
            with httpx.Client(verify=self.config.tls_context(), transport=self.transport,
                              trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(20, connect=5)) as client:
                with client.stream("POST", self.config.origin.rstrip("/") + PATH,
                    headers={"Authorization": "Bearer " + self.token}, json=request.model_dump()) as response:
                    if response.status_code != 200:
                        raise ValueError()
                    raw = bytearray()
                    for part in response.iter_bytes(chunk_size=65536):
                        raw.extend(part)
                        if len(raw) > MAX_READ_BYTES:
                            raise ValueError()
            return bound_input(request.intent, json.loads(raw))
        except Exception:
            raise ValueError("authenticated_initial_read_unavailable") from None


def model_input(value):
    if (value.schema_version != 2 or value.analysis.grade != "code_change"
            or value.analysis.inventory_digest != value.binding.inventory_digest
            or value.binding.stage != "PROPOSE" or value.binding.purpose != "t38-initial-proposal"
            or value.manifest.source_sha != value.binding.source_sha):
        raise ValueError("initial_model_input_conflict")
    bundle = FailureBundle(run_id=value.run_id, source_sha=value.binding.source_sha,
        failure_code="CODE_CHANGE_REQUIRED", summary=value.analysis.rationale,
        evidence_digest=value.analysis.inventory_digest)
    # No admission_digest is invented. Only T11's existing worker value shape is used.
    return SimpleNamespace(bundle=bundle, manifest=value.manifest, files=value.files)


def stage_initial(value, root):
    if set(value.files) != set(value.manifest.files):
        raise ValueError("initial_source_conflict")
    source = root / "source"
    source.mkdir(mode=0o700)
    for name, content in value.files.items():
        if hashlib.sha256(content.encode()).hexdigest() != value.manifest.files[name]:
            raise ValueError("initial_source_conflict")
        target = source / name  # SnapshotManifest has already checked every path.
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content.encode())
        target.chmod(0o400)
    snapshot = Snapshot(source, value.manifest)
    for name in value.files:
        snapshot.read(name)  # Reuse source policy and hash checks, including secret literals.
    return snapshot


def checked_result(result, request, value, snapshot):
    result = InitialResult.model_validate_json(json.dumps(result, allow_nan=False))
    expected = (request.intent.request_id, request.intent.input_digest,
                request.intent.runtime_source_sha, f"github-{request.github_run_id}-1")
    if ((result.request_id, result.input_digest, result.runtime_source_sha, result.execution_receipt_id) != expected
            or result.schema_valid is not True or result.lint_passed is not True
            or result.proposal_digest != digest(result.proposal)):
        raise ValueError("initial_result_conflict")
    Tools(model_input(value).bundle, snapshot, 1).dispatch("mcp__bravo__plan_propose", result.proposal.model_dump())
    return result.model_dump(mode="json")


@dataclass
class InitialProposalService:
    journal: object
    authenticate: object = field(repr=False)
    reader: object = field(repr=False)  # Request-scoped factory; keeps the token in memory.
    invoke: object = field(repr=False)
    runtime_source_sha: str

    def probe(self, body):
        # Initial proposal tokens cannot invoke a rehearsal probe by accident.
        return 404, stopped("UNKNOWN_ROUTE")

    def run(self, body):
        try:
            request = InitialBrokerRequest.model_validate(body)
            if request.intent.runtime_source_sha != self.runtime_source_sha:
                raise ValueError()
        except Exception:
            return 400, stopped("INVALID_INITIAL_REQUEST")
        try:
            access = self.authenticate(request.web_identity_token)
            if type(access) is not ProductAccess:
                raise ValueError()
            lookup = InitialRead(principal=access.principal, intent=request.intent,
                                 github_run_id=request.github_run_id)
            read = self.reader(request.web_identity_token)
            value = bound_input(request.intent, read(lookup).model_dump(mode="json"))
            prepared = model_input(value)
        except Exception:
            return 403, stopped("INITIAL_INPUT_OR_ACCESS_UNAVAILABLE")
        try:
            binding = digest(lookup)
            with TemporaryDirectory(prefix="initial-", dir=self.journal.root) as staging:
                root = Path(staging)
                snapshot = stage_initial(value, root)
                key = "initial:" + value.run_id
                fresh, saved = self.journal.claim(key, binding)
                if not fresh:
                    if saved is None or saved.get("state") == "NEEDS_HUMAN":
                        return 409, stopped("INITIAL_OUTCOME_UNKNOWN")
                    return 200, checked_result(saved, request, value, snapshot)
                try:
                    raw = self.invoke(access, prepared, root, snapshot)
                    # Reuse T11 schema, immutable source, allowed paths and policy lint.
                    checked = proposal_result(raw, prepared, snapshot, {})
                    # A slow model call must not retain expired/revoked T10 authority.
                    if bound_input(request.intent, read(lookup).model_dump(mode="json")) != value:
                        raise ValueError()
                    result = InitialResult(request_id=request.intent.request_id,
                        input_digest=request.intent.input_digest, runtime_source_sha=self.runtime_source_sha,
                        execution_receipt_id=f"github-{request.github_run_id}-1", proposal=checked["proposal"],
                        proposal_digest=checked["proposal_digest"], schema_valid=True, lint_passed=True)
                    response = checked_result(result.model_dump(), request, value, snapshot)
                except Exception:
                    response = stopped("INITIAL_RUNTIME_UNCONFIRMED")
                self.journal.finish(key, binding, response)
                return (409 if response.get("state") == "NEEDS_HUMAN" else 200), response
        except Exception:
            return 409, stopped("INITIAL_INPUT_OR_JOURNAL_UNAVAILABLE")
