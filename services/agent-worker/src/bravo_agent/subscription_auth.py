"""App-owned SIWC registration and renewal, independent of Codex's login cache."""

import base64
import hashlib
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
JWKS = ISSUER + "/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
APP_NAME = "bravo_platform_agent"
SCOPES = frozenset({"openid", "profile", "email", "offline_access", "resource.invoke",
                    "chatgpt.tokens.use.direct"})
TERMINAL_REFRESH = frozenset({"invalid_grant", "invalid_refresh_token", "token_expired",
                              "refresh_token_expired", "refresh_token_invalidated",
                              "refresh_token_reused"})


class AuthFailure(RuntimeError):
    """Only static error codes cross the operator/log boundary."""


class ReauthenticationRequired(AuthFailure):
    pass


@dataclass(repr=False)
class LoginAttempt:
    redirect_uri: str
    host_id: str
    client_id: str = "dynamic_agent_client"
    subject: str | None = None
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    nonce: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(64))
    expires_at: float = field(default_factory=lambda: time.time() + 600)
    consumed: bool = False

    def authorization_url(self) -> str:
        uri = urlsplit(self.redirect_uri)
        if (uri.scheme != "http" or uri.hostname != "127.0.0.1" or not uri.port
                or uri.path != "/auth/callback" or uri.query or uri.fragment or uri.username):
            raise AuthFailure("INVALID_CALLBACK_URI")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": self.client_id, "ext_agent_host_id": self.host_id,
                  "response_type": "code", "redirect_uri": self.redirect_uri,
                  "scope": " ".join(sorted(SCOPES)), "resource": RESOURCE,
                  "state": self.state, "nonce": self.nonce,
                  "code_challenge_method": "S256", "code_challenge": challenge}
        if self.client_id == "dynamic_agent_client":
            params["agent_name_hint"] = APP_NAME
        # No id_token_hint in a URL; returning accounts still reuse their issued client ID.
        return AUTHORIZE + "?" + urlencode(params)

    def consume(self, callback_path: str) -> tuple[str, str]:
        if self.consumed or time.time() >= self.expires_at:
            raise AuthFailure("EXPIRED_OR_USED_LOGIN")
        self.consumed = True
        uri = urlsplit(callback_path)
        values = parse_qs(uri.query, keep_blank_values=True, max_num_fields=20)
        if (uri.path != "/auth/callback" or uri.scheme or uri.netloc or uri.fragment
                or any(len(v) != 1 for v in values.values())
                or not secrets.compare_digest(values.get("state", [""])[0], self.state)):
            raise AuthFailure("INVALID_OAUTH_STATE")
        if "error" in values:
            raise AuthFailure("OAUTH_DECLINED")
        client_id = values.get("client_id", [self.client_id])[0]
        if (not client_id.startswith("oaiapp_") or len(client_id) > 200
                or (self.client_id != "dynamic_agent_client" and client_id != self.client_id)):
            raise AuthFailure("CLIENT_REGISTRATION_MISMATCH")
        code = values.get("code", [""])[0]
        if not code or len(code) > 8192:
            raise AuthFailure("MISSING_AUTHORIZATION_CODE")
        return code, client_id


class OAuth:
    def __init__(self, http=None):
        self.http = http or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)

    def _json(self, response, *, limit=131072):
        if len(response.content) > limit:
            raise AuthFailure("AUTH_RESPONSE_TOO_LARGE")
        try:
            data = response.json()
        except ValueError:
            raise AuthFailure("INVALID_AUTH_RESPONSE") from None
        if not isinstance(data, dict):
            raise AuthFailure("INVALID_AUTH_RESPONSE")
        return data

    def _token(self, form, *, refreshing=False):
        response = self.http.post(TOKEN, data=form)
        data = self._json(response)
        if response.status_code != 200:
            if refreshing and data.get("error") in TERMINAL_REFRESH:
                raise ReauthenticationRequired("SUBSCRIPTION_REAUTH_REQUIRED")
            raise AuthFailure("OAUTH_TOKEN_REQUEST_FAILED")
        return data

    def _identity(self, token, client_id, nonce=None, subject=None):
        try:
            response = self.http.get(JWKS)
            if response.status_code != 200:
                raise AuthFailure("JWKS_UNAVAILABLE")
            keys = self._json(response)["keys"]
            kid = jwt.get_unverified_header(token).get("kid")
            matches = [k for k in keys if k.get("kid") == kid and k.get("use", "sig") == "sig"]
            if not kid or len(matches) != 1:
                raise AuthFailure("UNRECOGNIZED_SIGNING_KEY")
            key = jwt.PyJWK.from_dict(matches[0]).key
            claims = jwt.decode(token, key, algorithms=["RS256", "ES256"],
                                audience=client_id, issuer=ISSUER,
                                options={"require": ["iss", "aud", "sub", "exp"]})
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise AuthFailure("INVALID_IDENTITY")
            if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
                raise AuthFailure("INVALID_OIDC_NONCE")
            if subject is not None and claims["sub"] != subject:
                raise AuthFailure("ADMIN_ACCOUNT_MISMATCH")
            return claims
        except (ValueError, KeyError, TypeError, jwt.PyJWTError):
            raise AuthFailure("INVALID_ID_TOKEN") from None

    def _record(self, data, client_id, runtime_host_id, *, nonce=None, subject=None):
        if (data.get("token_type", "").lower() != "bearer"
                or any(not isinstance(data.get(k), str) or not data[k] or len(data[k]) > 32768
                       for k in ("access_token", "refresh_token", "id_token"))
                or type(data.get("expires_in")) is not int or not 300 < data["expires_in"] <= 86400
                or not isinstance(data.get("scope"), str)):
            raise AuthFailure("INCOMPLETE_TOKEN_SET")
        granted = set(data["scope"].split())
        identity = self._identity(data["id_token"], client_id, nonce, subject)
        earliest = data.get("earliest_refresh_at", 0)
        if type(earliest) not in (int, float) or not math.isfinite(earliest) or earliest < 0:
            raise AuthFailure("INVALID_REFRESH_WINDOW")
        now = time.time()
        record = {"schema_version": 1, "state": "ACTIVE", "issuer": ISSUER,
                "client_id": client_id, "subject": identity["sub"],
                "email": identity.get("email"), "runtime_host_id": runtime_host_id,
                "access_token": data["access_token"], "refresh_token": data["refresh_token"],
                "id_token": data["id_token"], "scopes": sorted(granted),
                "expires_at": now + data["expires_in"], "earliest_refresh_at": earliest,
                "saved_at": now}
        # Keep the verified account/client binding after a declined plan grant. A later
        # explicit login reuses this registration rather than creating another app.
        return record if SCOPES <= granted else cleared_record(record, "PLAN_DISABLED")

    def exchange(self, attempt: LoginAttempt, callback_path: str, runtime_host_id: str):
        code, client_id = attempt.consume(callback_path)
        data = self._token({"grant_type": "authorization_code", "client_id": client_id,
                            "code": code, "code_verifier": attempt.verifier,
                            "redirect_uri": attempt.redirect_uri, "resource": RESOURCE})
        return self._record(data, client_id, runtime_host_id,
                            nonce=attempt.nonce, subject=attempt.subject)

    def refresh(self, record):
        data = self._token({"grant_type": "refresh_token", "client_id": record["client_id"],
                            "refresh_token": record["refresh_token"], "resource": RESOURCE},
                           refreshing=True)
        return self._record(data, record["client_id"], record["runtime_host_id"],
                            subject=record["subject"])

    def models(self, access_token):
        response = self.http.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + access_token})
        if response.status_code != 200:
            raise AuthFailure("SUBSCRIPTION_MODEL_CATALOG_UNAVAILABLE")
        # The account catalog includes model instructions, well beyond the small
        # OAuth/JWKS envelope. Keep a separate bounded size without retaining it.
        models = self._json(response, limit=2097152).get("models")
        if not isinstance(models, list):
            raise AuthFailure("INVALID_MODEL_CATALOG")
        return [item["slug"] for item in models if isinstance(item, dict)
                and item.get("visibility") == "list"
                and isinstance(item.get("slug"), str)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item["slug"])]

    def revoke(self, record):
        response = self.http.get(ISSUER + "/.well-known/openid-configuration")
        if response.status_code != 200:
            raise AuthFailure("REVOCATION_DISCOVERY_FAILED")
        discovery = self._json(response)
        endpoint = discovery.get("revocation_endpoint", "")
        uri = urlsplit(endpoint)
        if (discovery.get("issuer") != ISSUER or uri.scheme != "https"
                or uri.netloc != "auth.openai.com" or uri.query or uri.fragment):
            raise AuthFailure("INVALID_REVOCATION_ENDPOINT")
        response = self.http.post(endpoint, data={"token": record["refresh_token"],
                                  "token_type_hint": "refresh_token", "client_id": record["client_id"]})
        if response.status_code != 200:
            raise AuthFailure("REMOTE_REVOCATION_UNCONFIRMED")


def cleared_record(record, state="SIGNED_OUT"):
    return {k: v for k, v in {**record, "state": state}.items()
            if k not in {"access_token", "refresh_token", "id_token", "expires_at", "earliest_refresh_at"}}
