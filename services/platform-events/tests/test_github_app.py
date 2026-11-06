"""GitHub App token: signed JWT, exact permissions, cached, no token on redirects."""

import base64
import io
import json
import urllib.error

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from bravo_platform_events.github_app import GitHubApp, GitHubError

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption())
PERMS = {"actions": "read"}


class Opener:
    def __init__(self, granted=PERMS):
        self.requests, self.granted = [], granted

    def open(self, request, timeout):
        self.requests.append(request)
        url = request.full_url
        if url.endswith("/installation"):
            body = {"id": 5}
        elif url.endswith("/access_tokens"):
            body = {"token": "ghs_fixture", "expires_at": "2026-11-06T14:00:00Z",
                    "permissions": self.granted | {"metadata": "read"}}
        elif url.endswith("/zip"):
            raise urllib.error.HTTPError(url, 302, "Found", {"Location": "https://storage.example/blob"}, None)
        else:
            body = {"ok": url}
        return io.BytesIO(json.dumps(body).encode())


def app(opener, now=1793970000.0):  # 2026-11-06T13:00Z
    return GitHubApp(42, PEM, "Softbank-Hackathon-Finals/demo-chat", PERMS, clock=lambda: now, opener=opener)


def test_jwt_is_signed_by_the_app_key_and_token_is_cached():
    opener = Opener()
    client = app(opener)
    assert json.loads(client.call("GET", "repos/Softbank-Hackathon-Finals/demo-chat/actions/runs/1"))["ok"]
    client.call("GET", "repos/Softbank-Hackathon-Finals/demo-chat/actions/runs/2")
    assert sum(r.full_url.endswith("/access_tokens") for r in opener.requests) == 1
    jwt = opener.requests[0].get_header("Authorization").split()[1]
    head, body, sig = jwt.split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    KEY.public_key().verify(pad(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(pad(body))["iss"] == "42"
    minted = json.loads(opener.requests[1].data)
    assert minted == {"repositories": ["demo-chat"], "permissions": PERMS}
    assert opener.requests[2].get_header("Authorization") == "Bearer ghs_fixture"


def test_wider_or_narrower_grant_is_refused():
    with pytest.raises(GitHubError):
        app(Opener(granted={"actions": "write"})).token()


def test_redirect_is_followed_without_the_token():
    opener = Opener()
    app(opener).call("GET", "repos/Softbank-Hackathon-Finals/demo-chat/actions/artifacts/3/zip")
    last = opener.requests[-1]
    assert last.full_url == "https://storage.example/blob" and last.get_header("Authorization") is None


def test_paths_outside_the_repository_are_refused():
    with pytest.raises(ValueError):
        app(Opener()).call("GET", "repos/Softbank-Hackathon-Finals/platform/actions/runs")
