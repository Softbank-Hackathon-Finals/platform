from urllib.error import HTTPError, URLError

import pytest

from rehearsal_runner.kube_secret_probe import observe_secret_get


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, *_):
        raise AssertionError("the secret body must not be read")


class Opener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def setup_probe(tmp_path, monkeypatch, result):
    token = tmp_path / "token"
    token.write_text("fixture-token")
    opener = Opener(result)
    monkeypatch.setattr("ssl.create_default_context", lambda **_: object())
    monkeypatch.setattr("urllib.request.build_opener", lambda *args: opener)
    return token, opener


def test_200_reports_access_without_reading_secret_body(tmp_path, monkeypatch):
    token, opener = setup_probe(tmp_path, monkeypatch, Response())
    assert observe_secret_get("rehearsal-x", "app-credentials", token_path=token, ca_path=token) == "yes"
    request, timeout = opener.requests[0]
    assert request.full_url == "https://kubernetes.default.svc/api/v1/namespaces/rehearsal-x/secrets/app-credentials"
    assert request.get_header("Authorization") == "Bearer fixture-token"
    assert timeout == 5


@pytest.mark.parametrize("status,expected", [(403, "no"), (401, "unknown"), (404, "unknown"),
                                               (302, "unknown"), (500, "unknown")])
def test_only_forbidden_is_denial(tmp_path, monkeypatch, status, expected):
    error = HTTPError("https://kubernetes.default.svc", status, "error", {}, None)
    token, _ = setup_probe(tmp_path, monkeypatch, error)
    assert observe_secret_get("rehearsal-x", "app-credentials", token_path=token, ca_path=token) == expected


def test_transport_failure_is_unknown(tmp_path, monkeypatch):
    token, _ = setup_probe(tmp_path, monkeypatch, URLError("not available"))
    assert observe_secret_get("rehearsal-x", "app-credentials", token_path=token, ca_path=token) == "unknown"


@pytest.mark.parametrize("namespace,secret", [("../default", "valid"), ("default", "x/y"),
                                                 ("default", "invalid..name"), ("BAD", "valid")])
def test_invalid_scopes_never_reach_api(tmp_path, monkeypatch, namespace, secret):
    token, opener = setup_probe(tmp_path, monkeypatch, Response())
    with pytest.raises(ValueError):
        observe_secret_get(namespace, secret, token_path=token, ca_path=token)
    assert opener.requests == []


def test_missing_token_is_unknown(tmp_path):
    assert observe_secret_get("default", "app", token_path=tmp_path / "missing", ca_path=tmp_path / "missing") == "unknown"
