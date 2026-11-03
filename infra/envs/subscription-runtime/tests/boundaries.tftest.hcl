mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
}

variables {
  oidc_provider_arn       = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"
  production_subject      = "repo:Softbank-Hackathon-Finals@350259663/platform@1443045223:ref:refs/heads/develop"
  validation_subject      = "repo:Softbank-Hackathon-Finals@350259663/platform@1443045223:ref:refs/heads/feature/T15-subscription-integration"
  validation_expires_at   = "2026-11-03T23:00:00Z"
  instance_arn            = "arn:aws:ec2:ap-northeast-2:111122223333:instance/i-0123456789abcdef0"
  subscription_secret_arn = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:bravo-platform/agent-subscription/example-ABC123"
}

run "credential_and_session_boundaries" {
  command = apply
  assert {
    condition     = jsondecode(aws_iam_role_policy.broker.policy).Statement[0].Resource == var.subscription_secret_arn && length(jsondecode(aws_iam_role_policy.broker.policy).Statement[0].Action) == 3
    error_message = "The VM broker only reads and rotates its exact subscription Secret."
  }
  assert {
    condition     = one([for statement in jsondecode(aws_iam_role_policy.controller.policy).Statement : statement if statement.Action == "ssm:StartSession"]).Condition.BoolIfExists["ssm:SessionDocumentAccessCheck"] == "true"
    error_message = "Omitting DocumentName must not grant a default shell session."
  }
  assert {
    condition     = jsondecode(aws_ssm_document.tunnel.content).properties.portNumber == "18761" && jsondecode(aws_ssm_document.tunnel.content).sessionType == "Port"
    error_message = "The tunnel must target the fixed loopback broker port."
  }
}
run "wildcard_secret_rejected" {
  command = plan
  variables { subscription_secret_arn = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:*" }
  expect_failures = [var.subscription_secret_arn]
}
run "wildcard_subject_rejected" {
  command = plan
  variables { production_subject = "repo:Softbank-Hackathon-Finals/platform:*" }
  expect_failures = [var.production_subject]
}
run "other_instance_region_rejected" {
  command = plan
  variables { instance_arn = "arn:aws:ec2:us-east-1:111122223333:instance/i-0123456789abcdef0" }
  expect_failures = [var.instance_arn]
}
