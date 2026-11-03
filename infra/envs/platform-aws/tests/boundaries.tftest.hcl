mock_provider "aws" {
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0", map_public_ip_on_launch = false, availability_zone = "ap-northeast-2a" }
  }
}
variables {
  account_id               = "111122223333"
  vpc_id                   = "vpc-0123456789abcdef0"
  private_subnet_id        = "subnet-0123456789abcdef0"
  private_route_table_id   = "rtb-0123456789abcdef0"
  ami_id                   = "ami-0123456789abcdef0"
  github_oidc_provider_arn = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"
  github_oidc_subjects     = ["repo:Softbank-Hackathon-Finals@1/platform@2:ref:refs/heads/feature/T7-rke2-oidc"]
}
run "private_host_and_constrained_actions" {
  command = apply
  assert {
    condition     = aws_instance.node.associate_public_ip_address == false && aws_instance.node.metadata_options[0].http_tokens == "required" && aws_instance.node.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "Private host and IMDSv2 hop-1 are required."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.actions.policy).Statement) == 2 && jsondecode(aws_iam_role_policy.actions.policy).Statement[0].Action == "ssm:SendCommand"
    error_message = "Actions may only invoke the fixed probe and read its result."
  }
  assert {
    condition     = jsondecode(aws_iam_role.actions.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "OIDC audience must be STS."
  }
  assert {
    condition     = !strcontains(aws_ssm_document.smoke.content, "{{") && !strcontains(aws_ssm_document.smoke.content, "parameters")
    error_message = "The SSM probe must not accept shell/manifest inputs."
  }
}
run "wildcard_subject_rejected" {
  command = plan
  variables { github_oidc_subjects = ["repo:Softbank-Hackathon-Finals/platform:*"] }
  expect_failures = [var.github_oidc_subjects]
}
run "foreign_repo_rejected" {
  command = plan
  variables { github_oidc_subjects = ["repo:another-owner/platform:ref:refs/heads/develop"] }
  expect_failures = [var.github_oidc_subjects]
}
run "public_subnet_rejected" {
  command = plan
  override_data {
    target = data.aws_subnet.private
    values = { vpc_id = "vpc-0123456789abcdef0", map_public_ip_on_launch = true }
  }
  expect_failures = [aws_instance.node]
}
