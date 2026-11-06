"""Product input contracts usable by T10 without importing a model runtime."""

import hashlib
import json
from typing import Annotated

from pydantic import Field

from .contracts import Contract, Digest, FailureBundle, SnapshotManifest, safe_text


class ProductRequest(Contract):
    input_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    input_digest: Digest
    web_identity_token: str = Field(min_length=1, max_length=16384, repr=False)


class ProductInput(Contract):
    """Resolved from a committed T10 Admission and registered immutable source.

    admission_digest covers all eight Admission fields. The authenticated host
    reader owns current plan/catalog/purpose/tenant authority, not this schema.
    """

    admission_digest: Digest
    attempt: int = Field(ge=1, le=3)
    bundle: FailureBundle
    manifest: SnapshotManifest
    files: dict[str, Annotated[str, Field(max_length=32768)]] = Field(min_length=1, max_length=64, repr=False)


def validate_product_input(value: ProductInput | dict) -> ProductInput:
    """Detach and validate source content; do not register, authorize or execute."""
    raw = value.model_dump(mode="json") if isinstance(value, ProductInput) else value
    value = ProductInput.model_validate_json(json.dumps(raw, allow_nan=False))
    if value.bundle.source_sha != value.manifest.source_sha or set(value.files) != set(value.manifest.files):
        raise ValueError("input source mismatch")
    for name, content in value.files.items():
        safe_text(content)
        data = content.encode("utf-8")
        if len(data) > 32768 or hashlib.sha256(data).hexdigest() != value.manifest.files[name]:
            raise ValueError("source content mismatch")
    return value
