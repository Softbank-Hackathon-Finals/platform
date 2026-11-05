"""Trusted broker TLS configuration, never supplied in a product request."""

import os
import ssl
from pathlib import Path


def client_context(ca_pem: str) -> ssl.SSLContext:
    if not isinstance(ca_pem, str) or not ca_pem or len(ca_pem) > 1048576:
        raise ValueError("reviewed broker CA required")
    # Explicit cadata excludes ambient system roots and SSL_CERT_FILE overrides.
    try:
        tls = ssl.create_default_context(cadata=ca_pem)
    except (ValueError, ssl.SSLError):
        raise ValueError("reviewed broker CA required") from None
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    return tls


def server_context() -> ssl.SSLContext:
    # systemd copies root-owned files into the service's private credential mount.
    directory = Path(os.environ["CREDENTIALS_DIRECTORY"])
    if not directory.is_absolute():
        raise ValueError("private TLS credential mount required")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(directory / "broker.crt", directory / "broker.key")
    return tls
