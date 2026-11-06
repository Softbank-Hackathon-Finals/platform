import copy
import json
import time
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from bravo_agent.subscription_auth import (
    ISSUER,
    JWKS,
    RESOURCE,
    SCOPES,
    TOKEN,
    AuthFailure,
    LoginAttempt,
    OAuth,
    ReauthenticationRequired,
)
from bravo_agent.subscription_store import (
    SubscriptionStore,
    broker_lock,
    credential_policy,
    host_id,
)

HOST = "urn:uuid:00000000-0000-4000-8000-000000000000"
ARN = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:subscription-example-abcdef"


@pytest.fixture
def signed_tokens():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="fixture", use="sig")

    def tokens(nonce="fixture-nonce", **overrides):
        claims = {"iss": ISSUER, "aud": "oaiapp_fixture", "sub": "fixture-subject",
                  "exp": int(time.time()) + 3600, "nonce": nonce}
        claims.update(overrides)
        return {"access_token": "access-fixture", "refresh_token": "refresh-fixture",
                "id_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture"}),
                "expires_in": 3600, "token_type": "Bearer", "scope": " ".join(sorted(SCOPES)),
                "earliest_refresh_at": 0}
    return tokens, public


def oauth_fixture(tokens, public, *, status=200):
    requests = []

    def handle(request):
        requests.append(request)
        if str(request.url) == JWKS:
            return httpx.Response(200, json={"keys": [public]})
        assert str(request.url) == TOKEN
        return httpx.Response(status, json=tokens)
    return OAuth(httpx.Client(transport=httpx.MockTransport(handle))), requests


def callback(attempt, **overrides):
    fields = {"state": attempt.state, "code": "one-time-fixture", "client_id": "oaiapp_fixture"}
    fields.update(overrides)
    return "/auth/callback?" + urlencode(fields)


def test_pkce_registration_validates_identity_and_preserves_vm_host(signed_tokens):
    tokens, public = signed_tokens
    attempt = LoginAttempt("http://127.0.0.1:1455/auth/callback", uuid.uuid4().urn)
    oauth, requests = oauth_fixture(tokens(attempt.nonce), public)
    url = parse_qs(urlsplit(attempt.authorization_url()).query)
    assert url["client_id"] == ["dynamic_agent_client"]
    assert url["ext_agent_host_id"] == [attempt.host_id]
    assert url["code_challenge_method"] == ["S256"]
    assert "client_secret" not in url
    record = oauth.exchange(attempt, callback(attempt), HOST)
    assert record["client_id"] == "oaiapp_fixture"
    assert record["subject"] == "fixture-subject" and record["runtime_host_id"] == HOST
    body = parse_qs(requests[0].content.decode())
    assert body["code_verifier"] == [attempt.verifier]
    assert body["resource"] == [RESOURCE]
    assert body["redirect_uri"] == [attempt.redirect_uri]
    assert body["client_id"] == ["oaiapp_fixture"]
    with pytest.raises(AuthFailure, match="EXPIRED_OR_USED_LOGIN"):
        oauth.exchange(attempt, callback(attempt), HOST)
    assert len(requests) == 2


@pytest.mark.parametrize("overrides", [{"nonce": "wrong"}, {"aud": "other-app"},
                                      {"iss": "https://example.invalid"}, {"exp": 1},
                                      {"sub": "other-subject"}])
def test_invalid_or_other_account_id_token_never_activates(signed_tokens, overrides):
    tokens, public = signed_tokens
    attempt = LoginAttempt("http://127.0.0.1:1455/auth/callback", HOST,
                           "oaiapp_fixture", "fixture-subject")
    kwargs = {"nonce": attempt.nonce, **overrides}
    oauth, _ = oauth_fixture(tokens(**kwargs), public)
    with pytest.raises(AuthFailure):
        oauth.exchange(attempt, callback(attempt), HOST)


@pytest.mark.parametrize("fields", [{"state": "wrong"}, {"error": "access_denied"},
                                   {"client_id": "oaiapp_other"}])
def test_bad_callback_is_rejected_before_exchange(signed_tokens, fields):
    tokens, public = signed_tokens
    attempt = LoginAttempt("http://127.0.0.1:1455/auth/callback", HOST,
                           "oaiapp_fixture", "fixture-subject")
    oauth, requests = oauth_fixture(tokens(attempt.nonce), public)
    with pytest.raises(AuthFailure):
        oauth.exchange(attempt, callback(attempt, **fields), HOST)
    assert requests == []


def test_unsigned_identity_rejected(signed_tokens):
    tokens, public = signed_tokens
    for mutate in (lambda d: d.update(id_token=jwt.encode({"sub": "fixture-subject"}, key="", algorithm="none")),):
        attempt = LoginAttempt("http://127.0.0.1:1455/auth/callback", HOST)
        data = tokens(attempt.nonce)
        mutate(data)
        oauth, _ = oauth_fixture(data, public)
        with pytest.raises(AuthFailure):
            oauth.exchange(attempt, callback(attempt), HOST)


def test_declined_plan_keeps_verified_registration_without_usable_tokens(signed_tokens):
    tokens, public = signed_tokens
    attempt = LoginAttempt("http://127.0.0.1:1455/auth/callback", HOST)
    data = tokens(attempt.nonce)
    data["scope"] = "openid profile email offline_access resource.invoke"
    oauth, _ = oauth_fixture(data, public)
    record = oauth.exchange(attempt, callback(attempt), HOST)
    assert record["state"] == "PLAN_DISABLED"
    assert record["client_id"] == "oaiapp_fixture" and record["subject"] == "fixture-subject"
    assert not {"access_token", "refresh_token", "id_token"} & record.keys()


def active_record():
    return {"schema_version": 1, "state": "ACTIVE", "issuer": ISSUER,
            "client_id": "oaiapp_fixture", "subject": "fixture-subject", "runtime_host_id": HOST,
            "access_token": "access-fixture", "refresh_token": "refresh-fixture", "id_token": "id-fixture",
            "scopes": list(SCOPES), "expires_at": time.time() + 3600, "earliest_refresh_at": 0}


class MemorySecrets:
    def __init__(self, record):
        self.current, self.records, self.calls = "old-version", {"old-version": record}, []

    def get_secret_value(self, **params):
        self.calls.append(("get", params))
        return {"SecretString": json.dumps(self.records[self.current]), "VersionId": self.current}

    def put_secret_value(self, **params):
        self.calls.append(("put", params))
        self.records[params["ClientRequestToken"]] = json.loads(params["SecretString"])

    def update_secret_version_stage(self, **params):
        self.calls.append(("stage", params))
        if params["VersionStage"] == "AWSCURRENT":
            if params["RemoveFromVersionId"] != self.current:
                raise RuntimeError("concurrent update")
            self.current = params["MoveToVersionId"]


def test_refresh_rotates_full_set_before_issuing_access(signed_tokens):
    tokens, public = signed_tokens
    record = active_record()
    record["expires_at"] = time.time() + 10
    data = tokens()
    data.update(access_token="renewed-access-fixture", refresh_token="rotated-refresh-fixture")
    oauth, requests = oauth_fixture(data, public)
    memory = MemorySecrets(record)
    store = SubscriptionStore(ARN, HOST, memory)
    assert store.access_token(oauth) == "renewed-access-fixture"
    saved = memory.records[memory.current]
    assert saved["refresh_token"] == "rotated-refresh-fixture"
    assert saved["client_id"] == record["client_id"] and saved["subject"] == record["subject"]
    body = parse_qs(requests[0].content.decode())
    assert body["grant_type"] == ["refresh_token"] and "scope" not in body
    assert [name for name, _ in memory.calls] == ["get", "put", "stage", "stage"]
    assert memory.calls[2][1]["RemoveFromVersionId"] == "old-version"


def test_concurrent_registration_is_not_overwritten():
    record = active_record()
    memory = MemorySecrets(record)
    store = SubscriptionStore(ARN, HOST, memory)
    _, version = store.read()
    memory.current = "admin-new-version"
    memory.records[memory.current] = {**record, "subject": "another-registered-admin"}
    with pytest.raises(RuntimeError, match="concurrent update"):
        store.replace(record, version)
    assert memory.current == "admin-new-version"


@pytest.mark.parametrize("code,terminal", [("invalid_grant", True), ("temporary_failure", False)])
def test_refresh_failure_stops_without_fallback_or_retry(signed_tokens, code, terminal):
    _, public = signed_tokens
    record = active_record()
    record["expires_at"] = time.time() + 10
    memory = MemorySecrets(copy.deepcopy(record))
    oauth, requests = oauth_fixture({"error": code, "error_description": "private-provider-detail"}, public, status=400)
    with pytest.raises(AuthFailure) as error:
        SubscriptionStore(ARN, HOST, memory).access_token(oauth)
    assert "private-provider-detail" not in str(error.value)
    assert len(requests) == 1
    current = memory.records[memory.current]
    if terminal:
        assert isinstance(error.value, ReauthenticationRequired)
        assert current["state"] == "REAUTH_REQUIRED" and "refresh_token" not in current
    else:
        assert current == record


def test_wrong_host_rejected_and_valid_access_does_not_refresh():
    memory = MemorySecrets(active_record())
    store = SubscriptionStore(ARN, HOST, memory)
    assert store.access_token(None) == "access-fixture"
    with pytest.raises(AuthFailure, match="CREDENTIAL_HOST_MISMATCH"):
        SubscriptionStore(ARN, uuid.uuid4().urn, memory).read()


def test_host_identity_stable_and_parallel_renewal_rejected(tmp_path):
    private = tmp_path / "broker"
    first = host_id(private)
    assert first == host_id(private)
    assert (private / "host-id").stat().st_mode & 0o077 == 0
    with broker_lock(private), pytest.raises(AuthFailure, match="BROKER_BUSY"), broker_lock(private):
        pytest.fail("second broker must not enter")


def test_private_paths_reject_symlink_and_world_readable_state(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    link = tmp_path / "linked"
    link.symlink_to(private, target_is_directory=True)
    with pytest.raises(AuthFailure):
        host_id(link)
    private.chmod(0o755)
    with pytest.raises(AuthFailure):
        host_id(private)


@pytest.mark.parametrize("mode", ["duplicate", "expired", "unregistered"])
def test_callback_replay_expiry_and_unregistered_client_fail_closed(mode):
    attempt = LoginAttempt("http://127.0.0.1:1455/auth/callback", HOST)
    path = callback(attempt)
    if mode == "duplicate":
        path += "&state=" + attempt.state
    elif mode == "expired":
        attempt.expires_at = 1
    else:
        path = callback(attempt, client_id="dynamic_agent_client")
    with pytest.raises(AuthFailure):
        attempt.consume(path)


@pytest.mark.parametrize("malicious", [False, True])
def test_revocation_pins_discovered_endpoint_and_never_forwards_to_other_host(malicious):
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"issuer": ISSUER, "revocation_endpoint":
                "https://untrusted.example/revoke" if malicious else ISSUER + "/oauth/revoke"})
        return httpx.Response(200, content=b"")
    oauth = OAuth(httpx.Client(transport=httpx.MockTransport(handle)))
    if malicious:
        with pytest.raises(AuthFailure, match="INVALID_REVOCATION_ENDPOINT"):
            oauth.revoke(active_record())
        assert len(requests) == 1
    else:
        oauth.revoke(active_record())
        body = parse_qs(requests[-1].content.decode())
        assert body["token"] == ["refresh-fixture"] and body["client_id"] == ["oaiapp_fixture"]


def test_active_login_does_not_restart_user_authentication(tmp_path):
    from bravo_agent.subscription_admin import login

    store = SubscriptionStore(ARN, HOST, MemorySecrets(active_record()))
    with pytest.raises(AuthFailure, match="ALREADY_CONNECTED"):
        login(store, tmp_path)


def test_broker_child_receives_only_access_token_and_fixed_inputs(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from bravo_agent import subscription_admin

    directory = tmp_path / "broker"
    runtime_host = host_id(directory)
    record = {**active_record(), "runtime_host_id": runtime_host}
    store = SubscriptionStore(ARN, runtime_host, MemorySecrets(record))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-fixture")
    monkeypatch.setenv("OPENAI_API_KEY", "never-fallback-fixture")
    monkeypatch.setenv("GH_TOKEN", "github-fixture")
    monkeypatch.setattr(subscription_admin.OAuth, "models", lambda self, token: ["fixture-model"])
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout="private-provider-detail", stderr="private-error")
    monkeypatch.setattr(subscription_admin.subprocess, "run", run)
    args = SimpleNamespace(model="fixture-model", **{k: tmp_path / k for k in
        ("bundle", "manifest", "snapshot", "output")})
    assert subscription_admin.run_worker(store, directory, args) == 0
    command, options = calls[0]
    assert command[1:6] == ["-m", "bravo_agent.cli", "analyze", "--provider", "chatgpt-plan"]
    assert options["env"]["ACCESS_TOKEN"] == "access-fixture"
    assert set(options["env"]) == {"PATH", "LANG", "HOME", "TMPDIR", "ACCESS_TOKEN", "PYTHONDONTWRITEBYTECODE"}
    assert options["timeout"] == 210 and options["capture_output"]
    output = capsys.readouterr().out
    assert "PROPOSAL_READY" in output and "private-" not in output and "fixture" not in output
    assert "--inventory" not in command  # T38 bridges build ``args`` without an inventory
    args.inventory = tmp_path / "inventory.json"
    assert subscription_admin.run_worker(store, directory, args) == 0
    command = calls[1][0]
    assert command[command.index("--inventory") + 1] == str(args.inventory.absolute())


def test_model_catalog_uses_subscription_access_and_filters_hidden_models():
    def handle(request):
        assert str(request.url) == RESOURCE + "/models"
        assert request.headers["Authorization"] == "Bearer access-fixture"
        return httpx.Response(200, json={"models": [{"slug": "fixture-model", "visibility": "list"},
                                                    {"slug": "hidden", "visibility": "hidden"}]})
    assert OAuth(httpx.Client(transport=httpx.MockTransport(handle))).models("access-fixture") == ["fixture-model"]


def test_broker_policy_is_exact_secret_and_has_no_execution_or_enumeration():
    statement = credential_policy(ARN)["Statement"][0]
    assert statement["Resource"] == ARN and "*" not in json.dumps(statement)
    assert set(statement["Action"]) == {"secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
                                        "secretsmanager:UpdateSecretVersionStage"}
    with pytest.raises(AuthFailure):
        credential_policy(ARN + "*")


def test_large_model_catalog_is_separate_from_small_auth_response_limit():
    response = httpx.Response(200, json={"models": [{"slug": "fixture-model", "visibility": "list",
                                                   "instructions": "x" * 650000}]})
    oauth = OAuth(httpx.Client(transport=httpx.MockTransport(lambda request: response)))
    assert oauth.models("access-fixture") == ["fixture-model"]
    with pytest.raises(AuthFailure, match="AUTH_RESPONSE_TOO_LARGE"):
        oauth._json(response)
