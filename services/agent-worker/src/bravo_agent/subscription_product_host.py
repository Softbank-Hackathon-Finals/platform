"""Host-only ports for registered product input and the fixed SSM tunnel.

This installs no reader, token supplier, broker, tunnel or workflow. T10 owns
current admission/catalog authority; only its authenticated read port may be
injected. The caller still needs the existing GitHub OIDC path to the broker.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from .contracts import FailureBundle, digest
from .product_contracts import ProductInput, ProductRequest, validate_product_input
from .subscription_product import stopped
from .verification import validate_result

_DIGEST = re.compile(r"[a-f0-9]{64}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_ADMISSION_FIELDS = {"plan_id", "tenant_id", "plan_version", "task_id", "rehearsal_id",
                     "attempt", "source_sha", "task_digest"}
_REGISTRATION_FIELDS = {"admission", "product_input", "input_digest", "principal", "purpose", "expires_at"}
_RESPONSE_FIELDS = {"product_input_digest", "admission_digest", "runtime_source_sha", "state",
                    "input_kind", "run_id", "source_sha", "attempt", "input", "input_digest",
                    "task", "task_digest", "snapshot_digest", "proposal", "proposal_digest",
                    "tool_events_status"}


@dataclass(frozen=True)
class RegisteredProductInputs:
    """Adapt T10's authenticated, current read(principal, ref) port to T38.

    The reader must authenticate its transport, check the committed Admission,
    current plan/tenant/version/catalog and purpose grant, and reject revoked
    registrations on every read. A signed/stored object alone cannot replace
    those current checks. Neither the reader nor its address comes from HTTP.
    """

    read: Callable[[str, str], dict] = field(repr=False)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    def resolve(self, principal: str, input_ref: str) -> ProductInput:
        try:
            if (not isinstance(principal, str) or _DIGEST.fullmatch(principal) is None
                    or not isinstance(input_ref, str) or _REF.fullmatch(input_ref) is None):
                raise ValueError("invalid lookup")
            record = json.loads(json.dumps(self.read(principal, input_ref), allow_nan=False))
            if type(record) is not dict or set(record) != _REGISTRATION_FIELDS:
                raise ValueError("invalid registration")
            admission = record["admission"]
            if (type(admission) is not dict or set(admission) != _ADMISSION_FIELDS
                    or any(type(admission[k]) is not str or not admission[k]
                           for k in _ADMISSION_FIELDS - {"plan_version", "attempt"})
                    or type(admission["plan_version"]) is not int or admission["plan_version"] < 1
                    or type(admission["attempt"]) is not int or admission["attempt"] != 1
                    or _DIGEST.fullmatch(admission["task_digest"]) is None):
                raise ValueError("invalid admission binding")
            expiry = record["expires_at"]
            if not isinstance(expiry, str) or not expiry.endswith(("Z", "+00:00")):
                raise ValueError("UTC expiry required")
            expires_at = datetime.fromisoformat(expiry)
            now = self.clock()  # after the reader, so a slow read cannot extend validity
            if (expires_at.utcoffset() is None or now.utcoffset() is None or expires_at <= now
                    or record["principal"] != principal or record["purpose"] != "t38-product-proposal"):
                raise ValueError("expired or unauthorized registration")
            # Detach nested dictionaries from the reader before the model sees them.
            value = validate_product_input(record["product_input"])
            if (digest(value) != record["input_digest"] or value.admission_digest != digest(admission)
                    or value.attempt != admission["attempt"]
                    or value.bundle.run_id != admission["rehearsal_id"]
                    or value.bundle.source_sha != admission["source_sha"]
                    or value.manifest.source_sha != admission["source_sha"]):
                raise ValueError("input does not match admission")
            return value
        except Exception:
            # A transport or source exception may carry a token or source text.
            raise ValueError("registered product input unavailable") from None


def validate_product_response(result: dict, request: ProductRequest, runtime_source_sha: str) -> dict:
    """Validate the broker's projection; T15 separately binds it to its input.

    No verification verdict or model audit is reconstructed here. The runtime
    SHA is the exact reviewed installation selected by trusted deployment code.
    """
    if type(result) is not dict or set(result) != _RESPONSE_FIELDS:
        raise ValueError("invalid product response")
    bundle, _ = validate_result(result)
    task = FailureBundle.model_validate(result["task"])
    if (result["input_kind"] != "product" or result["product_input_digest"] != request.input_digest
            or result["runtime_source_sha"] != runtime_source_sha
            or not isinstance(result["admission_digest"], str)
            or _DIGEST.fullmatch(result["admission_digest"]) is None
            or task != bundle or result["task_digest"] != digest(task)
            or result["tool_events_status"] != "UNAVAILABLE"):
        raise ValueError("unbound product response")
    return result


@dataclass(frozen=True)
class ProductTunnelCall:
    """The #291 call(body) port, only inside an already established SSM tunnel.

    No arbitrary endpoint, environment proxy, redirects, token acquisition or
    retry. The host owns the tunnel and single dispatch window. After response
    loss, an operator may read the same admitted call through the durable broker
    journal; this transport never starts another call automatically.
    """

    runtime_source_sha: str
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def __post_init__(self):
        if not isinstance(self.runtime_source_sha, str) or re.fullmatch(r"[a-f0-9]{40}", self.runtime_source_sha) is None:
            raise ValueError("exact runtime source required")

    def __call__(self, body: dict) -> tuple[int, dict]:
        try:
            request = ProductRequest.model_validate(body)
            with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(260, connect=5)) as client:
                with client.stream("POST", "http://127.0.0.1:18762/run", json=request.model_dump()) as response:
                    # The remaining status payload is deliberately not echoed.
                    if response.status_code != 200:
                        return response.status_code, stopped("PRODUCT_CALL_UNCONFIRMED")
                    data = bytearray()
                    for part in response.iter_bytes(chunk_size=65536):
                        data.extend(part)
                        if len(data) > 1024 * 1024:
                            raise ValueError("oversized product response")
            result = validate_product_response(json.loads(data), request, self.runtime_source_sha)
            return 200, result
        except Exception:
            return 502, stopped("PRODUCT_CALL_UNCONFIRMED")
