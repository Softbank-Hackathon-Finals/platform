# T6 출발지: demo-chat 로그인용 Cognito 사용자 풀.
# 앱은 표준 OIDC(발급자 주소 + 공개 클라이언트 ID)만 안다. 그래서 이 풀은 "설정 교체" 등급의 AWS 의존성이다.
# 사용자는 이 스택이 만들지 않는다. 비밀번호가 Terraform state에 남지 않게 운영자가 CLI로 만든다(README).

resource "aws_cognito_user_pool" "demo" {
  name                     = var.name
  deletion_protection      = "INACTIVE" # 데모 뒤 terraform destroy로 지운다
  mfa_configuration        = "OFF"
  auto_verified_attributes = []

  username_configuration {
    case_sensitive = false
  }

  # 스스로 가입하지 못한다. 시연 계정은 운영자만 만든다.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = false
    temporary_password_validity_days = 3 # 시연 전날 만든 계정이 당일 만료되지 않게
  }
}

resource "aws_cognito_user_pool_domain" "demo" {
  domain       = var.domain_prefix
  user_pool_id = aws_cognito_user_pool.demo.id
}

# 브라우저가 쓰는 공개 클라이언트: 비밀 없음, Authorization Code + PKCE만.
resource "aws_cognito_user_pool_client" "demo_chat" {
  name         = "demo-chat"
  user_pool_id = aws_cognito_user_pool.demo.id

  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  # demo-chat은 두 발급자에 같은 scope(openid profile email)를 요청한다. 여기서 email을 빼면 Cognito가
  # 로그인 요청을 invalid_scope로 거절한다. 사용자에게 email이 없으면 토큰에도 없을 뿐이다.
  allowed_oauth_scopes         = ["openid", "profile", "email"]
  supported_identity_providers = ["COGNITO"]
  # aws.cognito.signin.user.admin을 넣으면 사용자가 자기 속성(이름)을 바꿀 수 있다. 앱은 이름으로 작성자를 정한다.
  callback_urls = [for origin in var.app_origins : "${origin}/"]
  logout_urls   = [for origin in var.app_origins : "${origin}/"]

  # 사용자 이름·비밀번호를 앱이 직접 받는 흐름은 열지 않는다. 로그인은 Cognito 화면에서만 한다.
  explicit_auth_flows           = ["ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"

  # 작성자 이름이 되는 속성(preferred_username · name · email · nickname)은 이 클라이언트로 쓸 수 없다.
  # 빈 목록은 "모두 허용"으로 읽히므로 이름과 무관한 속성만 둔다.
  write_attributes        = ["locale", "zoneinfo"]
  enable_token_revocation = true

  id_token_validity      = var.id_token_minutes
  access_token_validity  = var.id_token_minutes
  refresh_token_validity = 1
  token_validity_units {
    id_token      = "minutes"
    access_token  = "minutes"
    refresh_token = "days"
  }
}
