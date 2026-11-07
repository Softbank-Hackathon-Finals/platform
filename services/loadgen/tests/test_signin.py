"""로그인을 켠 앱에 쓰기 부하를 넣을 때의 ID 토큰 공급 (T47). 발급자는 가짜로 둔다."""

import asyncio
import base64
import json
import sys

import httpx
import pytest

from bravo_loadgen import signin
from bravo_loadgen.signin import Issued, SignInError, TokenSupply

ISSUER = "https://login.example.com/realms/bravo"
COGNITO = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_Demo123"
PASSWORD = "do-not-print-this-password"
SECRET = "do-not-print-this-secret"
ENV = {signin.PASSWORD_ENV: PASSWORD, signin.CLIENT_SECRET_ENV: SECRET}


def jwt(**claims):
    """서명은 보지 않으므로 아무 값이나 둔다. 앱이 서명을 확인한다."""
    claims = {"iat": 1_700_000_000, "exp": 1_700_003_600, **claims}

    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256', 'kid': 'k1'})}.{part(claims)}.c2lnbmF0dXJl"


def test_name_follows_demo_chat_order_and_length():
    # demo-chat app/auth.py display_name: preferred_username → cognito:username → email → name → sub, 40자.
    assert signin.issued(jwt(preferred_username="loadgen", **{"cognito:username": "other"})).name == "loadgen"
    assert signin.issued(jwt(**{"cognito:username": "loadgen"}, email="x@example.test")).name == "loadgen"
    assert signin.issued(jwt(preferred_username="  ", name="Load Gen")).name == "Load Gen"
    assert signin.issued(jwt(preferred_username=" " + "가" * 50)).name == "가" * 40


def test_lifetime_comes_from_the_token_not_the_local_clock():
    assert signin.issued(jwt(preferred_username="loadgen")).lifetime_s == 3600


@pytest.mark.parametrize(
    "token",
    [
        "not-a-token",
        jwt(preferred_username="loadgen") + "\r\nX-Injected: 1",
        jwt(preferred_username="loadgen", exp=None),
        jwt(preferred_username="loadgen", exp=1_700_000_000),
        jwt(sub=""),
        None,
    ],
)
def test_unusable_tokens_are_refused_before_any_write(token):
    with pytest.raises(SignInError):
        signin.issued(token)


class FakeCognito:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response or {"AuthenticationResult": {"IdToken": jwt(**{"cognito:username": "loadgen"})}}
        self.error = error

    def admin_initiate_auth(self, **request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.response


def cognito_fetch(client, environ=ENV):
    kind, fetch = signin.fetcher(COGNITO, "loadgenclient", "loadgen", environ, cognito_client=client)
    assert kind == "cognito"
    return fetch


def test_cognito_uses_the_signed_admin_flow_of_the_issuers_pool():
    client = FakeCognito()
    assert cognito_fetch(client)().name == "loadgen"
    assert client.calls == [{
        "UserPoolId": "ap-northeast-2_Demo123",
        "ClientId": "loadgenclient",
        "AuthFlow": "ADMIN_USER_PASSWORD_AUTH",
        "AuthParameters": {"USERNAME": "loadgen", "PASSWORD": PASSWORD},
    }]


def test_cognito_needs_no_client_secret():
    assert cognito_fetch(FakeCognito(), {signin.PASSWORD_ENV: PASSWORD})().name == "loadgen"


def test_account_waiting_for_a_new_password_is_named_not_bypassed():
    fetch = cognito_fetch(FakeCognito(response={"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": "s"}))
    with pytest.raises(SignInError, match="NEW_PASSWORD_REQUIRED"):
        fetch()


def test_cognito_refusal_keeps_only_the_error_code():
    class ClientError(Exception):
        def __init__(self):
            super().__init__(f"An error occurred (NotAuthorizedException): {PASSWORD}")
            self.response = {"Error": {"Code": "NotAuthorizedException", "Message": PASSWORD}}

    with pytest.raises(SignInError) as refused:
        cognito_fetch(FakeCognito(error=ClientError()))()
    assert "NotAuthorizedException" in str(refused.value) and PASSWORD not in str(refused.value)
    assert refused.value.__cause__ is None and refused.value.__suppress_context__


def test_cognito_without_boto3_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(SignInError, match="--extra cognito"):
        signin.fetcher(COGNITO, "loadgenclient", "loadgen", ENV)


def oidc_fetch(handler, environ=ENV):
    kind, fetch = signin.fetcher(ISSUER, "demo-chat-loadgen", "loadgen", environ,
                                 transport=httpx.MockTransport(handler))
    assert kind == "oidc"
    return fetch


def keycloak(seen, *, token_endpoint=f"{ISSUER}/protocol/openid-connect/token", issuer=ISSUER, token=None):
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": issuer, "token_endpoint": token_endpoint})
        return token or httpx.Response(200, json={"id_token": jwt(preferred_username="loadgen")})

    return handler


def test_oidc_uses_the_password_grant_of_the_load_generator_client():
    seen = []
    assert oidc_fetch(keycloak(seen))().name == "loadgen"
    discovery, grant = seen
    assert str(discovery.url) == f"{ISSUER}/.well-known/openid-configuration"
    assert grant.method == "POST" and str(grant.url) == f"{ISSUER}/protocol/openid-connect/token"
    assert dict(httpx.QueryParams(grant.content.decode())) == {
        "grant_type": "password", "scope": "openid", "client_id": "demo-chat-loadgen",
        "client_secret": SECRET, "username": "loadgen", "password": PASSWORD,
    }


@pytest.mark.parametrize("missing", [signin.PASSWORD_ENV, signin.CLIENT_SECRET_ENV])
def test_oidc_needs_both_secrets_from_the_environment(missing):
    environ = {k: v for k, v in ENV.items() if k != missing}
    with pytest.raises(SignInError, match=missing):
        signin.fetcher(ISSUER, "demo-chat-loadgen", "loadgen", environ)


def test_credentials_never_leave_for_another_origin():
    seen = []
    fetch = oidc_fetch(keycloak(seen, token_endpoint="https://elsewhere.example/token"))
    with pytest.raises(SignInError):
        fetch()
    assert len(seen) == 1  # discovery only; nothing was posted


def test_discovery_naming_another_issuer_is_refused():
    with pytest.raises(SignInError):
        oidc_fetch(keycloak([], issuer="https://login.example.com/realms/other"))()


def test_refused_grant_reports_the_error_code_only():
    refusal = httpx.Response(401, json={"error": "unauthorized_client", "error_description": PASSWORD})
    with pytest.raises(SignInError) as refused:
        oidc_fetch(keycloak([], token=refusal))()
    message = str(refused.value)
    assert "401" in message and "unauthorized_client" in message
    assert PASSWORD not in message and SECRET not in message


def test_a_non_object_answer_is_named():
    with pytest.raises(SignInError, match="JSON 객체"):
        oidc_fetch(keycloak([], token=httpx.Response(200, json=[])))()


def test_redirects_are_not_followed_with_credentials():
    moved = httpx.Response(307, headers={"Location": "https://elsewhere.example/token"})
    seen = []
    with pytest.raises(SignInError):
        oidc_fetch(keycloak(seen, token=moved))()
    assert [r.url.host for r in seen] == ["login.example.com", "login.example.com"]


@pytest.mark.parametrize("issuer", ["http://login.example.com/realms/bravo", "ftp://x", "https://u:p@login.example.com"])
def test_issuer_must_be_https_except_on_this_machine(issuer):
    with pytest.raises(SignInError):
        signin.fetcher(issuer, "demo-chat-loadgen", "loadgen", ENV)


def test_plain_http_is_allowed_for_a_local_test_issuer():
    kind, _ = signin.fetcher("http://127.0.0.1:18183/realms/bravo", "demo-chat-loadgen", "loadgen", ENV)
    assert kind == "oidc"


def token(name="loadgen", lifetime_s=3600.0, value="a.b.c"):
    return Issued(id_token=value, name=name, lifetime_s=lifetime_s)


def test_first_token_is_taken_before_the_load_and_sets_the_author():
    supply = TokenSupply(lambda: token(value="first.token.value"))
    assert supply.name == "loadgen"
    assert supply.headers() == {"Authorization": "Bearer first.token.value"}


def test_first_failure_stops_the_run():
    def refuse():
        raise SignInError("no")

    with pytest.raises(SignInError):
        TokenSupply(refuse)


def run_supply(supply, seconds):
    async def go():
        task = asyncio.create_task(supply.keep_fresh())
        await asyncio.sleep(seconds)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


def test_token_is_replaced_before_it_expires():
    issued = iter([token(lifetime_s=0.2, value="one.one.one"), token(lifetime_s=60, value="two.two.two")])
    supply = TokenSupply(lambda: next(issued))
    run_supply(supply, 0.3)  # 수명 절반(0.1초)이 지나면 다시 받는다
    assert supply.headers() == {"Authorization": "Bearer two.two.two"}
    assert supply.summary("oidc") == {"kind": "oidc", "refreshes": 1, "refresh_failures": 0}


def test_failed_refresh_keeps_the_current_token_and_is_counted(monkeypatch):
    monkeypatch.setattr(signin, "RETRY_S", 0.05)
    answers = iter([token(lifetime_s=0.1, value="one.one.one"), SignInError("down"), SignInError("down"),
                    token(lifetime_s=60, value="two.two.two")])

    def fetch():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    supply = TokenSupply(fetch)
    run_supply(supply, 0.3)
    assert supply.headers() == {"Authorization": "Bearer two.two.two"}
    assert supply.summary("oidc") == {"kind": "oidc", "refreshes": 1, "refresh_failures": 2}


def test_a_renamed_account_is_not_adopted_mid_run(monkeypatch):
    """작성자는 실행 내내 같아야 한다. 이름이 바뀐 토큰으로 바꾸면 재시도가 409 · 403이 된다."""
    monkeypatch.setattr(signin, "RETRY_S", 10)
    issued = iter([token(lifetime_s=0.1, value="one.one.one"), token(name="someone", value="two.two.two")])
    supply = TokenSupply(lambda: next(issued))
    run_supply(supply, 0.2)
    assert supply.headers() == {"Authorization": "Bearer one.one.one"}
    assert supply.summary("oidc")["refresh_failures"] == 1
