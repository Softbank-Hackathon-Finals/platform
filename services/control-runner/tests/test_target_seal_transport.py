import io
import json
import ssl

import pytest
from test_openbao import cert as cert

from bravo_controls.target_secret_encryption import OpenBaoSealReader


def test_seal_reader_has_one_tokenless_fixed_verified_request(cert, monkeypatch):
    from bravo_controls import target_secret_encryption as module

    seen = []
    stream = io.BytesIO(b'{"sealed":false}')

    class Connection:
        def __init__(self, host, port, *, timeout, context):
            assert (host, port, timeout) == ("localhost", 8200, 3)
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname
            assert context.minimum_version >= ssl.TLSVersion.TLSv1_2

        def request(self, method, path, headers):
            seen.append((method, path, headers))

        def getresponse(self):
            return self

        status = 200

        def getheader(self, name, default=None):
            return {"Content-Type": "application/json", "Content-Encoding": "identity"}.get(name, default)

        def read(self, size):
            assert size == 16385
            return stream.read(size)

        def close(self):
            seen.append("closed")

    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    reader = OpenBaoSealReader("https://localhost:8200", cert[0].read_text())
    assert reader() == {"sealed": False}
    assert seen == [("GET", "/v1/sys/seal-status", {"Accept": "application/json"}), "closed"]


@pytest.mark.parametrize("status,encoding,mime,body", [
    (307, "identity", "application/json", b'{}'),
    (500, "identity", "application/json", b'{}'),
    (200, "gzip", "application/json", b'{}'),
    (200, "identity", "text/html", b'{}'),
    (200, "identity", "application/json", b'{"sealed":true,"sealed":false}'),
    (200, "identity", "application/json", b'{"sealed":NaN}'),
    (200, "identity", "application/json", b'x' * 16385),
])
def test_ambiguous_http_response_has_no_redirect_retry_or_payload_disclosure(cert, monkeypatch, status, encoding, mime, body):
    from bravo_controls import target_secret_encryption as module

    seen = []

    class Connection:
        def __init__(self, *a, **kw):
            self.status = status

        def request(self, *a, **kw):
            seen.append("request")

        def getresponse(self):
            return self

        def getheader(self, name, default=None):
            return {"Content-Type": mime, "Content-Encoding": encoding}.get(name, default)

        def read(self, size):
            return body

        def close(self):
            seen.append("closed")

    monkeypatch.setattr(module.http.client, "HTTPSConnection", Connection)
    with pytest.raises(ValueError, match="^OpenBao seal observation unavailable$"):
        OpenBaoSealReader("https://localhost:8200", cert[0].read_text())()
    assert seen == ["request", "closed"]


@pytest.mark.parametrize("origin", [
    "http://localhost:8200", "https://user@localhost", "https://localhost/path",
    "https://localhost?token=x", "https://localhost#fragment", "https://localhost:0",
])
def test_unregistered_or_plaintext_endpoints_rejected_before_connection(cert, origin):
    with pytest.raises(ValueError):
        OpenBaoSealReader(origin, cert[0].read_text())


def test_token_field_is_not_part_of_reader_configuration(cert):
    reader = OpenBaoSealReader("https://localhost:8200", cert[0].read_text())
    assert set(vars(reader)) == {"origin", "ca_pem", "timeout"}
    assert "BEGIN CERTIFICATE" not in repr(reader)
    assert "Token" not in json.dumps(sorted(vars(reader)))
