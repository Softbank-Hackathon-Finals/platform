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
# 부하 생성기 설정. 비밀이 아니다. 앱의 AUTH_CLIENT_ID에는 넣지 않는다(브라우저는 이 클라이언트를 쓰지 않는다).
output "loadgen_client_id" {
  description = "bravo-loadgen --auth-client-id. Do not add it to AUTH_ALSO_TRUST before demo-app #30 is deployed: the older app keeps one client per issuer (README)."
  value       = aws_cognito_user_pool_client.loadgen.id
}
output "user_pool_arn" {
  description = "loadgen-aws source_user_pool_arn: the only pool the load generator may call AdminInitiateAuth on."
  value       = aws_cognito_user_pool.demo.arn
}
output "login_domain" {
  value = "https://${aws_cognito_user_pool_domain.demo.domain}.auth.${var.region}.amazoncognito.com"
}
