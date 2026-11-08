mock_provider "aws" {
  mock_resource "aws_cognito_user_pool" {
    defaults = { id = "ap-northeast-2_Demo123" }
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

run "outputs_are_the_app_settings" {
  command = apply
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
      "aws_cognito_user_pool_domain",
    ])
    error_message = "Only the pool, its domain and the public client are declared here."
  }
}
