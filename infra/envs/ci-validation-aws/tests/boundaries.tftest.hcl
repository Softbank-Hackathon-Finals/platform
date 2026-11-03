mock_provider "aws" {}

variables {
  account_id = "123456789012"
  ami_id     = "ami-0123456789abcdef0"
  deployment = "test-boundaries"
  expires_at = "2026-11-03T20:00:00Z"
}

run "sandbox_host_boundary" {
  command = plan
  assert {
    condition = length(aws_security_group.host.ingress) == 0 && length(aws_default_security_group.closed.ingress) == 0 && alltrue([
      for rule in aws_vpc_security_group_egress_rule.dns : rule.cidr_ipv4 == "10.92.0.2/32" && rule.to_port == 53
    ]) && aws_vpc_security_group_egress_rule.https.to_port == 443
    error_message = "The host must have no inbound access; outbound is HTTPS and VPC DNS only."
  }
  assert {
    condition     = aws_instance.validation.metadata_options[0].http_tokens == "required" && aws_instance.validation.metadata_options[0].http_put_response_hop_limit == 1 && aws_instance.validation.key_name == ""
    error_message = "Require IMDSv2, disallow routed metadata tokens, and do not create an SSH login."
  }
  assert {
    condition     = aws_iam_role_policy_attachment.ssm.policy_arn == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" && jsondecode(aws_iam_role.ssm.assume_role_policy).Statement[0].Principal.Service == "ec2.amazonaws.com"
    error_message = "The validation host only needs an EC2 SSM role, not application or model credentials."
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.deny_credentials.policy).Statement[0].Effect == "Deny" && alltrue([
      for action in ["ssm:GetParameter", "ssm:GetParameters", "secretsmanager:GetSecretValue", "kms:Decrypt"] : contains(jsondecode(aws_iam_role_policy.deny_credentials.policy).Statement[0].Action, action)
    ])
    error_message = "SSM managed policy must not enable application/model parameter or secret reads."
  }
  assert {
    condition     = aws_instance.validation.root_block_device[0].encrypted && aws_instance.validation.root_block_device[0].delete_on_termination && aws_instance.validation.instance_initiated_shutdown_behavior == "terminate" && strcontains(aws_instance.validation.user_data, "--on-active=90m")
    error_message = "Expiry must terminate the host and delete its encrypted temporary disk."
  }
  assert {
    condition     = strcontains(aws_instance.validation.user_data, "--disable traefik --disable servicelb") && !strcontains(aws_instance.validation.user_data, "--disable-network-policy")
    error_message = "Kubernetes NetworkPolicy must remain enabled; no public ingress stack is required."
  }
}

run "reject_long_lifetime" {
  command = plan
  variables { ttl_minutes = 91 }
  expect_failures = [var.ttl_minutes]
}

run "reject_wrong_account_shape" {
  command = plan
  variables { account_id = "1234" }
  expect_failures = [var.account_id]
}

run "reject_wrong_region" {
  command = plan
  variables { region = "us-east-1" }
  expect_failures = [var.region]
}
