# 앱 설정에 그대로 넣는 값. 비밀이 아니다(공개 클라이언트 ID와 공개 발급자 주소).
output "auth_issuer" {
  description = "AUTH_ISSUER for demo-chat."
  value       = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.demo.id}"
}
output "auth_client_id" {
  description = "AUTH_CLIENT_ID for demo-chat."
  value       = aws_cognito_user_pool_client.demo_chat.id
}
output "user_pool_id" {
  value = aws_cognito_user_pool.demo.id
}
output "login_domain" {
  value = "https://${aws_cognito_user_pool_domain.demo.domain}.auth.${var.region}.amazoncognito.com"
}
