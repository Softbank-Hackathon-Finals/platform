"""GitHub App installation token for one repository, with only the permissions asked for.

The App private key is a protected mount (platform OpenBao via ESO); it never enters
Git, logs, argv or the environment. Tokens are minted per call site and expire within
an hour. Redirects (artifact downloads) are followed without the Authorization header.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime

import jwt
from cryptography.hazmat.primitives import serialization

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
           "User-Agent": "bravo-platform-workflow-host"}


class GitHubError(Exception):
    def __init__(self, status, path):
        self.status = status
        super().__init__(f"github_{status}:{path.split('?')[0]}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHubApp:
    def __init__(self, app_id, private_key_pem, repository, permissions, *, clock=time.time, opener=None):
        if type(app_id) is not int or app_id <= 0 or repository.count("/") != 1 or not permissions:
            raise ValueError("github_app_configuration_required")
        self.key = serialization.load_pem_private_key(private_key_pem, password=None)
        self.app_id, self.repository, self.permissions = app_id, repository, dict(permissions)
        self.clock = clock
        self.opener = opener or urllib.request.build_opener(_NoRedirect)
        self._token, self._expires = None, 0.0

    def _jwt(self):
        now = int(self.clock())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}, self.key, algorithm="RS256")

    def _send(self, method, url, *, auth=None, body=None, limit=2 * 1024 * 1024):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method, headers=dict(HEADERS))
        if auth:
            request.add_header("Authorization", f"Bearer {auth}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 307) and error.headers.get("Location"):
                # Signed storage URL: fetch it without our token.
                return self._send("GET", error.headers["Location"], limit=limit)
            raise GitHubError(error.code, url) from None
        if len(raw) > limit:
            raise GitHubError(413, url)
        return raw

    def token(self):
        if self._token and self._expires - 300 > self.clock():
            return self._token
        jwt = self._jwt()
        installation = json.loads(self._send("GET", f"{API}/repos/{self.repository}/installation", auth=jwt))
        minted = json.loads(self._send("POST", f"{API}/app/installations/{installation['id']}/access_tokens",
            auth=jwt, body={"repositories": [self.repository.split("/")[1]], "permissions": self.permissions}))
        granted = dict(minted.get("permissions") or {})
        granted.pop("metadata", None)  # GitHub adds metadata:read to every token
        if granted != {k: v for k, v in self.permissions.items() if k != "metadata"}:
            raise GitHubError(403, "installation_permissions_differ")  # never accept wider or narrower
        expires = minted["expires_at"].replace("Z", "+00:00")
        self._token, self._expires = minted["token"], datetime.fromisoformat(expires).timestamp()
        return self._token

    def call(self, method, path, body=None, *, limit=2 * 1024 * 1024):
        if not path.startswith(f"repos/{self.repository}/"):
            raise ValueError("github_path_outside_repository")
        return self._send(method, f"{API}/{path}", auth=self.token(), body=body, limit=limit)
