"""Consume T38 product proposals (#280) as the bucket planner's `ProposalSource`.

The T10 host resolves the committed product input and calls T38 (in process or
over HTTP); this module only checks that the response is bound to that input,
then replays the proposal through a local T11 Coordinator so T11 policy is
enforced again here. The model's own tool events stay on the remote side, so
they are reported UNAVAILABLE rather than rebuilt from the local replay.
T38 supports only the first attempt; later attempts stop before any effect.
"""

import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .bucket_proposer import bucket_summary
from .bucket_task import BucketPolicyTask

ProductCall = Callable[[dict], tuple[int, dict]]


def bucket_product_bundle(task: BucketPolicyTask, source_policy_path: str, target_policy_path: str):
    """The first-attempt T11 input for a bucket task; the host puts it in `ProductInput.bundle`."""
    from bravo_agent.contracts import FailureBundle

    return FailureBundle(run_id=task.run_id, source_sha=task.source_sha, failure_code="POLICY_TRANSLATION",
                         evidence_digest="0" * 64,
                         summary=bucket_summary(task, source_policy_path, target_policy_path))


@dataclass(frozen=True)
class _RemoteProvider:
    call: ProductCall = field(repr=False)
    body: dict = field(repr=False)
    value: object = field(repr=False)
    tool_events_available = False  # the model ran remotely; local replay is not its audit

    async def propose(self, bundle, tools) -> None:
        from bravo_agent.contracts import digest

        if tools.attempt != 1:
            raise ValueError("T38 product path supports only the first attempt")
        status, result = self.call(dict(self.body))
        value = self.value
        if (status != 200 or not isinstance(result, dict) or result.get("state") != "PROPOSAL_READY"
                or result.get("input_kind") != "product" or result.get("attempt") != 1
                or result.get("run_id") != bundle.run_id or result.get("source_sha") != bundle.source_sha
                or bundle != value.bundle or result.get("input_digest") != digest(value.bundle)
                or result.get("snapshot_digest") != digest(value.manifest)
                or result.get("admission_digest") != value.admission_digest
                or result.get("product_input_digest") != self.body.get("input_digest")
                or result.get("tool_events_status") != "UNAVAILABLE"
                or not isinstance(result.get("proposal"), dict)
                or result.get("proposal_digest") != digest(result["proposal"])):
            raise ValueError("T38 response is not bound to this admitted input")
        # Local T11 re-validation; a policy denial here stops the attempt.
        tools.dispatch("mcp__bravo__plan_propose", result["proposal"])


@dataclass(frozen=True)
class RemoteProposalSource:
    """`ProposalSource.open` backed by T38. Every port is supplied by the T10 host.

    product_input(admission, task) -> the same `ProductInput` T38 resolves
    request_for(admission, value) -> {"input_ref", "input_digest", "web_identity_token"}
    call(body) -> (status, response) from `ProductService.run` or `POST /run`
    """

    product_input: Callable[[object, object], object] = field(repr=False)
    request_for: Callable[[object, object], dict] = field(repr=False)
    call: ProductCall = field(repr=False)
    staging: Path = Path(tempfile.gettempdir())

    def open(self, admission: object, task: object) -> tuple[object, object]:
        from bravo_agent.contracts import digest
        from bravo_agent.coordinator import Coordinator
        from bravo_agent.subscription_product import stage_input

        value = self.product_input(admission, task)
        if (value.attempt != 1 or value.bundle.run_id != admission.rehearsal_id
                or value.bundle.run_id != task.run_id or value.bundle.source_sha != task.source_sha):
            raise ValueError("product input does not match the admitted task")
        body = self.request_for(admission, value)
        if (not isinstance(body, dict) or set(body) != {"input_ref", "input_digest", "web_identity_token"}
                or body["input_digest"] != digest(value)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", str(body["input_ref"])) is None):
            raise ValueError("product request is not bound to the input")
        root = Path(tempfile.mkdtemp(prefix="t15-product-", dir=self.staging))
        # T38's own input contract: files must equal the manifest (safe paths only),
        # contents are checked, and nothing is written before those checks pass.
        snapshot = stage_input(value, root)
        coordinator = Coordinator(value.bundle, snapshot)
        return coordinator, _RemoteProvider(self.call, body, value)
