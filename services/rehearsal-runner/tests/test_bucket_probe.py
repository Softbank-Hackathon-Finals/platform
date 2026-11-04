import hashlib
import io

import pytest

from rehearsal_runner.bucket_probe import ReadResult, observe_bucket_reads


class Reader:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, bucket, key):
        self.calls.append((bucket, key))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def known(body=b"test canary"):
    return ReadResult(200, io.BytesIO(body))


def observe(app, anonymous):
    return observe_bucket_reads(app, anonymous, bucket="test-bucket", key="canary",
                                expected_sha256=hashlib.sha256(b"test canary").hexdigest())


def test_brackets_anonymous_denial_with_same_known_object():
    app, anonymous = Reader(known(), known()), Reader(ReadResult(403))
    result = observe(app, anonymous)
    assert (result.authenticated_before, result.anonymous, result.authenticated_after) == (
        "matches", "denied", "matches")
    assert app.calls == [("test-bucket", "canary")] * 2
    assert anonymous.calls == [("test-bucket", "canary")]


def test_public_read_is_observed_and_body_closed():
    body = io.BytesIO(b"public")
    result = observe(Reader(known(), known()), Reader(ReadResult(200, body)))
    assert result.anonymous == "allowed"
    assert body.closed


@pytest.mark.parametrize("response", [ReadResult(404), ReadResult(500), ReadResult(401)])
def test_ambiguous_anonymous_response_is_unknown(response):
    assert observe(Reader(known(), known()), Reader(response)).anonymous == "unknown"


def test_auth_object_drift_cannot_be_mistaken_for_known_object():
    result = observe(Reader(known(), known(b"changed")), Reader(ReadResult(403)))
    assert result.authenticated_after == "unknown"


def test_transport_error_is_unknown_and_does_not_log_exception():
    result = observe(Reader(RuntimeError("credential material")), Reader(ReadResult(403)))
    assert (result.authenticated_before, result.anonymous, result.authenticated_after) == (
        "unknown", "unknown", "unknown")
    assert "credential" not in repr(result)


def test_unbounded_or_invalid_inputs_are_rejected():
    with pytest.raises(ValueError):
        observe_bucket_reads(Reader(), Reader(), bucket="b", key="k", expected_sha256="x" * 64)


def test_missing_object_cannot_confirm_denial():
    result = observe(Reader(ReadResult(404), ReadResult(404)), Reader(ReadResult(403)))
    assert (result.authenticated_before, result.anonymous, result.authenticated_after) == (
        "unknown", "denied", "unknown")


def test_oversized_canary_is_unknown_and_stream_is_closed():
    body = io.BytesIO(b"x" * 4097)
    result = observe(Reader(ReadResult(200, body), known()), Reader(ReadResult(403)))
    assert result.authenticated_before == "unknown"
    assert body.closed
