"""Exercise actual signed/unsigned S3 HTTP requests against a local test server."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import boto3
import pytest

from rehearsal_runner.bucket_probe import BucketObservations, observe_bucket_reads
from rehearsal_runner.bucket_s3 import bucket_readers

DATA = b"test object contents"


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            signed = bool(self.headers.get("Authorization"))
            self.server.observed.append((signed, self.path))
            status = 200 if signed else self.server.anonymous_status
            self.send_response(status)
            self.end_headers()
            if status == 200:
                self.wfile.write(DATA)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.observed = []
    httpd.anonymous_status = 403
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize("status,expected", [(403, "denied"), (200, "allowed"), (404, "unknown")])
def test_signed_and_unsigned_requests_observe_same_object(server, status, expected):
    server.anonymous_status = status
    session = boto3.Session(aws_access_key_id="fixture-key", aws_secret_access_key="fixture-secret")
    signed, anonymous = bucket_readers(
        session, endpoint_url=f"http://127.0.0.1:{server.server_port}", region="test-region-1",
    )
    result = observe_bucket_reads(signed, anonymous, bucket="rehearsal-test", key="canary",
                                  expected_sha256=hashlib.sha256(DATA).hexdigest())
    assert result == BucketObservations("matches", expected, "matches")
    assert server.observed == [(True, "/rehearsal-test/canary"),
                               (False, "/rehearsal-test/canary"),
                               (True, "/rehearsal-test/canary")]


def test_wrong_known_object_digest_cannot_appear_as_a_success(server):
    session = boto3.Session(aws_access_key_id="fixture-key", aws_secret_access_key="fixture-secret")
    signed, anonymous = bucket_readers(
        session, endpoint_url=f"http://127.0.0.1:{server.server_port}", region="test-region-1",
    )
    result = observe_bucket_reads(signed, anonymous, bucket="rehearsal-test", key="canary",
                                  expected_sha256="0" * 64)
    assert result == BucketObservations("unknown", "denied", "unknown")


@pytest.mark.parametrize("url", ["ftp://example.org", "http://user:pass@example.org",
                                  "http://example.org/path", "http://example.org/?secret=1"])
def test_invalid_endpoints_are_rejected_before_client_creation(url):
    with pytest.raises(ValueError, match="endpoint"):
        bucket_readers(None, endpoint_url=url, region="test-region-1")
