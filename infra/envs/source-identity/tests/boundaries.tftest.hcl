mock_provider "aws" {
  mock_resource "aws_cognito_user_pool" {
    defaults = { id = "ap-northeast-2_Demo123", arn = "arn:aws:cognito-idp:ap-northeast-2:111122223333:userpool/ap-northeast-2_Demo123" }
  }
  mock_resource "aws_cognito_user_pool_client" {
    defaults = { id = "democlientid0123456789" }
  }
}
variables {
  account_id    = "111122223333"
  domain_prefix = "bravo-demo-chat-login"
  app_origins   = ["https://chat.example.com"]
}

# Cognito에는 공개 클라이언트에 PKCE를 강제하는 설정이 없다. 여기서는 "비밀 없음 + code 흐름"만 고정한다.
run "public_client_without_secret_uses_code_flow_only" {
  command = apply
  assert {
    condition     = aws_cognito_user_pool_client.demo_chat.generate_secret == false && aws_cognito_user_pool_client.demo_chat.allowed_oauth_flows == toset(["code"])
    error_message = "The browser client has no secret and only the authorization code flow."
  }
  assert {
    condition     = aws_cognito_user_pool_client.demo_chat.explicit_auth_flows == toset(["ALLOW_REFRESH_TOKEN_AUTH"])
    error_message = "The app never receives passwords: no USER_PASSWORD, SRP or admin auth flows."
  }
  assert {
    condition     = aws_cognito_user_pool_client.demo_chat.callback_urls == toset(["https://chat.example.com/"]) && aws_cognito_user_pool_client.demo_chat.supported_identity_providers == toset(["COGNITO"])
    error_message = "Callbacks are exactly the app origins' root."
  }
  assert {
    condition     = aws_cognito_user_pool.demo.admin_create_user_config[0].allow_admin_create_user_only == true
    error_message = "No self sign-up: only the operator creates demo accounts."
  }
  assert {
    condition     = aws_cognito_user_pool_client.demo_chat.prevent_user_existence_errors == "ENABLED" && aws_cognito_user_pool_client.demo_chat.logout_urls == toset(["https://chat.example.com/"])
    error_message = "Sign-in errors do not reveal accounts; logout returns only to the app."
  }
}

# demo-chat은 preferred_username → cognito:username → email → name 순으로 작성자 이름을 정한다.
# 사용자가 그 속성을 바꿀 수 있으면 다른 사람 이름으로 쓸 수 있다.
run "users_cannot_rename_themselves" {
  command = apply
  assert {
    condition     = aws_cognito_user_pool_client.demo_chat.allowed_oauth_scopes == toset(["openid", "profile", "email"]) && !contains(aws_cognito_user_pool_client.demo_chat.allowed_oauth_scopes, "aws.cognito.signin.user.admin")
    error_message = "No aws.cognito.signin.user.admin scope: tokens from this client cannot update user attributes."
  }
  assert {
    condition     = length(aws_cognito_user_pool_client.demo_chat.write_attributes) > 0 && length(setintersection(aws_cognito_user_pool_client.demo_chat.write_attributes, ["preferred_username", "name", "email", "nickname", "given_name", "family_name"])) == 0
    error_message = "write_attributes is explicit (empty means all) and holds no attribute the app can use as a name."
  }
}

# 부하 생성기 클라이언트를 더하면서 브라우저 클라이언트가 넓어지지 않았는지 한곳에서 고정한다.
run "browser_client_stays_as_reviewed" {
  command = apply
  assert {
    condition = (
      aws_cognito_user_pool_client.demo_chat.generate_secret == false &&
      aws_cognito_user_pool_client.demo_chat.allowed_oauth_flows_user_pool_client == true &&
      aws_cognito_user_pool_client.demo_chat.allowed_oauth_flows == toset(["code"]) &&
      aws_cognito_user_pool_client.demo_chat.allowed_oauth_scopes == toset(["openid", "profile", "email"]) &&
      aws_cognito_user_pool_client.demo_chat.explicit_auth_flows == toset(["ALLOW_REFRESH_TOKEN_AUTH"]) &&
      aws_cognito_user_pool_client.demo_chat.write_attributes == toset(["locale", "zoneinfo"])
    )
    error_message = "The browser client keeps exactly its reviewed flows, scopes and writable attributes; the load generator has its own client."
  }
}

# 부하 생성기는 사람 없이 토큰을 받는다. 그 길은 AWS 서명이 필요한 관리자 흐름 하나뿐이고, 브라우저 흐름은 없다.
run "loadgen_client_only_takes_signed_admin_sign_in" {
  command = apply
  assert {
    condition = (
      aws_cognito_user_pool_client.loadgen.generate_secret == false &&
      aws_cognito_user_pool_client.loadgen.allowed_oauth_flows_user_pool_client == false &&
      length(aws_cognito_user_pool_client.loadgen.allowed_oauth_flows) == 0 &&
      length(aws_cognito_user_pool_client.loadgen.allowed_oauth_scopes) == 0 &&
      length(aws_cognito_user_pool_client.loadgen.callback_urls) == 0 &&
      length(aws_cognito_user_pool_client.loadgen.logout_urls) == 0
    )
    error_message = "No hosted sign-in, OAuth flow, scope or redirect: visitors cannot sign in through the load generator client."
  }
  assert {
    condition     = aws_cognito_user_pool_client.loadgen.explicit_auth_flows == toset(["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"])
    error_message = "Only AdminInitiateAuth (AWS-signed) and refresh: no USER_PASSWORD, SRP, custom or choice-based flow that a client ID alone can call."
  }
  assert {
    condition     = length(aws_cognito_user_pool_client.loadgen.write_attributes) > 0 && length(setintersection(aws_cognito_user_pool_client.loadgen.write_attributes, ["preferred_username", "name", "email", "nickname", "given_name", "family_name"])) == 0
    error_message = "Tokens from this client cannot rename the account either (empty write_attributes means all)."
  }
  assert {
    condition     = aws_cognito_user_pool_client.loadgen.refresh_token_validity == 60 && aws_cognito_user_pool_client.loadgen.token_validity_units[0].refresh_token == "minutes" && aws_cognito_user_pool_client.loadgen.id_token_validity == 60
    error_message = "The generator never refreshes; keep the refresh token at Cognito's 60-minute minimum and the ID token as short as the browser's."
  }
  assert {
    condition     = aws_cognito_user_pool_client.loadgen.prevent_user_existence_errors == "ENABLED" && aws_cognito_user_pool_client.loadgen.enable_token_revocation == true
    error_message = "Sign-in errors do not reveal accounts, and its tokens can be revoked."
  }
}

run "rejects_localhost_callbacks_for_a_demo_pool" {
  command = plan
  variables {
    app_origins = ["http://localhost:8080"]
  }
  expect_failures = [var.app_origins]
}

run "accepts_localhost_callbacks_only_when_asked" {
  command = plan
  variables {
    app_origins           = ["http://localhost:8080"]
    allow_local_callbacks = true
  }
}

# state_key: the override only reaches a client being created, not one left by the apply runs above.
run "outputs_are_the_app_settings" {
  command   = apply
  state_key = "outputs"
  override_resource {
    target = aws_cognito_user_pool_client.loadgen
    values = { id = "loadgenclientid0123456" }
  }
  assert {
    condition     = output.loadgen_client_id == "loadgenclientid0123456" && output.user_pool_arn == "arn:aws:cognito-idp:ap-northeast-2:111122223333:userpool/ap-northeast-2_Demo123"
    error_message = "The load generator gets its own client ID, and loadgen-aws gets the pool ARN to scope AdminInitiateAuth to."
  }
  assert {
    condition     = output.auth_issuer == "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_Demo123" && output.auth_client_id == "democlientid0123456789"
    error_message = "AUTH_ISSUER and AUTH_CLIENT_ID come straight from the pool and client."
  }
}

run "rejects_a_reserved_domain_prefix" {
  command = plan
  variables {
    domain_prefix = "bravo-cognito-login"
  }
  expect_failures = [var.domain_prefix]
}

run "rejects_plain_http_public_origins" {
  command = plan
  variables {
    app_origins = ["http://chat.example.com"]
  }
  expect_failures = [var.app_origins]
}

run "source_declares_only_reviewed_resources" {
  command = plan
  # 사용자(aws_cognito_user)를 여기서 만들면 임시 비밀번호가 state에 남는다. 목록을 넓히려면 리뷰가 필요하다.
  assert {
    condition = sort(flatten([for name in fileset(path.module, "*.tf") : [
      for match in regexall("(?m)^resource \"([a-z0-9_]+)\"", file("${path.module}/${name}")) : match[0]
      ]])) == sort([
      "aws_cognito_user_pool",
      "aws_cognito_user_pool_client",
      "aws_cognito_user_pool_client",
      "aws_cognito_user_pool_domain",
    ])
    error_message = "Only the pool, its domain, the public browser client and the load generator client are declared here."
  }
}
