"""로그인을 켠 demo-chat에 쓰기 부하를 넣을 때 쓰기에 붙일 ID 토큰 (T47).

기본은 꺼져 있다. 켜면 부하 생성기 전용 클라이언트로 토큰을 받는다. 브라우저 클라이언트(`demo-chat`)는
비밀번호를 받지 않는다(source-identity · target-workloads README). 받는 방법은 발급자 주소로 정한다.

- Cognito(`https://cognito-idp.<리전>.amazonaws.com/<풀 ID>`): AdminInitiateAuth. 이 컴퓨터의 AWS 자격 증명으로
  서명한다(loadgen-aws 역할). 클라이언트 비밀은 없다.
- 그 밖의 표준 OIDC(목적지 Keycloak): 발급자 문서의 토큰 주소에 password grant. 클라이언트 비밀이 필요하다.

비밀번호 · 클라이언트 비밀 · 토큰은 환경변수와 메모리에만 둔다. 기록 · 리포트 · 오류 메시지에 넣지 않는다.
"""

import asyncio
import base64
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx

PASSWORD_ENV = "BRAVO_LOADGEN_PASSWORD"
CLIENT_SECRET_ENV = "BRAVO_LOADGEN_CLIENT_SECRET"
# demo-chat app/auth.py display_name과 같은 순서 · 길이. 서버는 작성자가 이 이름과 다르면 403을 준다.
NAME_CLAIMS = ("preferred_username", "cognito:username", "email", "name", "sub")
MAX_NAME = 40
COGNITO_ISSUER = re.compile(r"https://cognito-idp\.([a-z0-9-]+)\.amazonaws\.com/(\1_[A-Za-z0-9]+)")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
# RFC 6750 b64token. 헤더에 넣을 수 없는 값은 쓰기를 보내기 전에 막는다.
BEARER = re.compile(r"[A-Za-z0-9._~+/-]+=*")
ERROR_CODE = re.compile(r"[a-z_]{1,64}")
REQUEST_TIMEOUT_S = 10
# 수명의 절반이 지나면 다시 받는다. 실패해도 남은 절반 동안 RETRY_S마다 다시 시도한다.
REFRESH_AT = 0.5
RETRY_S = 5.0


class SignInError(LookupError):
    """토큰을 받지 못했다. 설정 오류와 같이 시험 불가로 다룬다. 메시지에 비밀 값을 넣지 않는다."""


@dataclass(frozen=True)
class Issued:
    id_token: str
    name: str
    lifetime_s: float


def display_name(claims: dict) -> str:
    for claim in NAME_CLAIMS:
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            return "".join(list(value.strip())[:MAX_NAME])
    raise SignInError("ID 토큰에 작성자로 쓸 이름이 없다")


def issued(id_token: object) -> Issued:
    """서명은 확인하지 않는다. 방금 TLS로 받은 토큰에서 이름과 수명만 읽는다. 확인은 앱이 한다."""
    if not isinstance(id_token, str) or not BEARER.fullmatch(id_token) or id_token.count(".") != 2:
        raise SignInError("ID 토큰 형식이 아니다")
    payload = id_token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        raise SignInError("ID 토큰 내용을 읽을 수 없다") from None
    if not isinstance(claims, dict):
        raise SignInError("ID 토큰 내용을 읽을 수 없다")
    exp, iat = claims.get("exp"), claims.get("iat")
    # 수명은 발급자가 적은 두 시각의 차이다. 이 컴퓨터의 시계와 무관하다.
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (exp, iat)) or exp <= iat:
        raise SignInError("ID 토큰에 수명(exp · iat)이 없다")
    return Issued(id_token, display_name(claims), float(exp - iat))


def fetcher(
    issuer: str,
    client_id: str,
    username: str,
    environ: Mapping[str, str] = os.environ,
    *,
    cognito_client=None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, Callable[[], Issued]]:
    """(발급자 종류, 토큰을 한 번 받는 함수). 비밀은 환경변수에서만 읽는다. 명령줄 인자는 프로세스 목록에 보인다."""
    password = _secret(environ, PASSWORD_ENV)
    if match := COGNITO_ISSUER.fullmatch(issuer):
        region, pool_id = match.groups()
        client = cognito_client or _cognito_client(region)
        return "cognito", lambda: _cognito(client, pool_id, client_id, username, password)
    url = _issuer_url(issuer)
    secret = _secret(environ, CLIENT_SECRET_ENV)
    return "oidc", lambda: _oidc(url, client_id, secret, username, password, transport)


def _secret(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not value:
        raise SignInError(f"로그인을 켜려면 환경변수 {name}가 필요하다")
    return value


def _issuer_url(issuer: str) -> httpx.URL:
    """demo-chat의 발급자 규칙과 같다: https, 이 컴퓨터의 시험 발급자만 http."""
    try:
        url = httpx.URL(issuer)
    except httpx.InvalidURL:
        raise SignInError("--auth-issuer가 올바른 주소가 아니다") from None
    local = url.scheme == "http" and url.host in LOCAL_HOSTS
    if url.userinfo or not url.host or (url.scheme != "https" and not local):
        raise SignInError("--auth-issuer는 자격 증명 없는 https 주소여야 한다")
    return url


def _cognito_client(region: str):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise SignInError("Cognito 로그인에는 boto3가 필요하다: uv sync --extra cognito") from None
    # 다시 받기는 TokenSupply가 RETRY_S마다 한다. SDK가 안에서 또 재시도하면 실행 끝에 그만큼 더 기다린다.
    config = Config(connect_timeout=REQUEST_TIMEOUT_S, read_timeout=REQUEST_TIMEOUT_S,
                    retries={"mode": "standard", "total_max_attempts": 1})
    return boto3.client("cognito-idp", region_name=region, config=config)


def _cognito(client, pool_id: str, client_id: str, username: str, password: str) -> Issued:
    try:
        response = client.admin_initiate_auth(
            UserPoolId=pool_id,
            ClientId=client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
        )
    except Exception as error:
        # botocore 오류 코드(NotAuthorizedException 등)만 남긴다. 메시지와 원인 사슬에는 요청 값이 섞일 수 있다.
        details = getattr(error, "response", None)
        code = details.get("Error", {}).get("Code") if isinstance(details, dict) else None
        raise SignInError(f"Cognito가 토큰을 주지 않았다: {code or type(error).__name__}") from None
    if challenge := response.get("ChallengeName"):
        # 첫 비밀번호 변경 같은 단계를 사람 대신 넘기지 않는다. 운영자가 영구 비밀번호를 정한다(README).
        raise SignInError(f"계정이 {challenge} 단계를 기다린다")
    return issued(response.get("AuthenticationResult", {}).get("IdToken"))


def _oidc(
    issuer: httpx.URL,
    client_id: str,
    secret: str,
    username: str,
    password: str,
    transport: httpx.BaseTransport | None,
) -> Issued:
    # 리다이렉트는 따라가지 않는다. 비밀번호를 실은 요청이 다른 곳으로 가지 않게 한다.
    with httpx.Client(timeout=REQUEST_TIMEOUT_S, trust_env=False, transport=transport) as http:
        try:
            document = _answer(http.get(f"{issuer}/.well-known/openid-configuration"), "발급자 문서")
            endpoint = document.get("token_endpoint")
            if document.get("issuer") != str(issuer) or not isinstance(endpoint, str):
                raise SignInError("발급자 문서가 다른 발급자를 가리키거나 토큰 주소가 없다")
            token_url = httpx.URL(endpoint)
            if (token_url.scheme, token_url.host, token_url.port) != (issuer.scheme, issuer.host, issuer.port):
                raise SignInError("토큰 주소가 발급자와 다른 곳이다. 비밀번호를 보내지 않는다")
            body = _answer(
                http.post(token_url, data={
                    "grant_type": "password", "scope": "openid", "client_id": client_id,
                    "client_secret": secret, "username": username, "password": password,
                }),
                "토큰 주소",
            )
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            raise SignInError(f"발급자에 닿지 못했다: {type(error).__name__}") from None
    return issued(body.get("id_token"))


def _answer(response: httpx.Response, what: str) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code == 200 and isinstance(body, dict):
        return body
    if response.status_code == 200:
        raise SignInError(f"{what}가 JSON 객체가 아닌 답을 줬다")
    # OAuth 오류 코드만 남긴다. error_description에는 계정 이름 같은 값이 들어갈 수 있다.
    error = body.get("error") if isinstance(body, dict) else None
    code = f" {error}" if isinstance(error, str) and ERROR_CODE.fullmatch(error) else ""
    raise SignInError(f"{what}가 {response.status_code}{code}로 답했다")


class TokenSupply:
    """토큰을 미리 다시 받아 둔다. 쓰기는 토큰을 기다리지 않는다.

    기다리면 그 시간이 응답 지연이나 예정 시각 지연으로 세어져, 대상이 느린 것처럼 보인다.
    """

    def __init__(self, fetch: Callable[[], Issued], *, clock: Callable[[], float] = time.monotonic):
        self._fetch = fetch
        self._clock = clock
        self.refreshes = 0
        self.refresh_failures = 0
        # 첫 토큰은 부하 전에 받는다. 못 받으면 SignInError로 시험 불가다.
        self._set(fetch())
        self.name = self._current.name

    def _set(self, token: Issued) -> None:
        self._current = token
        self._refresh_at = self._clock() + token.lifetime_s * REFRESH_AT

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._current.id_token}"}

    async def keep_fresh(self) -> None:
        """취소될 때까지 돈다."""
        while True:
            await asyncio.sleep(max(self._refresh_at - self._clock(), 0))
            try:
                token = await asyncio.to_thread(self._fetch)
                if token.name != self.name:
                    # 작성자는 실행 내내 같아야 한다. 바꾸면 같은 멱등 키의 재시도가 409 · 403이 된다.
                    raise SignInError("다시 받은 토큰의 이름이 다르다")
            except Exception:
                # 지금 토큰은 수명이 아직 남았다. 부하를 멈추지 않고 세기만 하고 다시 시도한다.
                self.refresh_failures += 1
                self._refresh_at = self._clock() + RETRY_S
                continue
            self.refreshes += 1
            self._set(token)

    def summary(self, kind: str) -> dict:
        return {"kind": kind, "refreshes": self.refreshes, "refresh_failures": self.refresh_failures}
