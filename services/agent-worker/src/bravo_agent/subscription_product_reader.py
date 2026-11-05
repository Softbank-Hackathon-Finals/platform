"""Operator-configured HTTPS reader; request-scoped Actions OIDC stays in memory."""

import json
import re
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

READ_PATH = "/internal/v1/product-inputs/read"
MAX_READ_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ProductReaderConfig:
    origin: str
    ca_file: str

    def __post_init__(self):
        if (not isinstance(self.origin, str) or not isinstance(self.ca_file, str)
                or any(c.isspace() for c in self.origin) or "\x00" in self.ca_file):
            raise ValueError("trusted HTTPS reader configuration required")
        url = urlsplit(self.origin)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment
                or not Path(self.ca_file).is_absolute() or "\n" in self.ca_file):
            raise ValueError("trusted HTTPS reader configuration required")
        # Trigger invalid-port validation before any token can be supplied.
        _ = url.port

    def tls_context(self):
        return ssl.create_default_context(cafile=self.ca_file)


@dataclass(frozen=True)
class ProductInputHTTPReader:
    config: ProductReaderConfig
    token: str = field(repr=False)
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def __call__(self, principal: str, input_ref: str) -> dict:
        try:
            if (not isinstance(principal, str) or re.fullmatch(r"[a-f0-9]{64}", principal) is None
                    or not isinstance(input_ref, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", input_ref) is None
                    or not isinstance(self.token, str) or not 1 <= len(self.token) <= 16384
                    or any(c.isspace() for c in self.token)):
                raise ValueError("invalid authenticated read")
            tls = self.config.tls_context()
            with httpx.Client(verify=tls, transport=self.transport, trust_env=False,
                              follow_redirects=False, timeout=httpx.Timeout(20, connect=5)) as client:
                with client.stream("POST", self.config.origin.rstrip("/") + READ_PATH,
                                   headers={"Authorization": "Bearer " + self.token},
                                   json={"principal": principal, "input_ref": input_ref}) as response:
                    if response.status_code != 200:
                        raise ValueError("reader did not accept request")
                    raw = bytearray()
                    for part in response.iter_bytes(chunk_size=65536):
                        raw.extend(part)
                        if len(raw) > MAX_READ_BYTES:
                            raise ValueError("oversized input record")
            value = json.loads(raw)
            if type(value) is not dict:
                raise ValueError("invalid record")
            return value
        except Exception:
            # Neither source text nor credentials from transport exceptions escape.
            raise ValueError("authenticated product read unavailable") from None
