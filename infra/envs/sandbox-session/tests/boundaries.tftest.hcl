mock_provider "aws" {
  mock_data "aws_instance" {
    defaults = { private_ip = "10.0.1.8", subnet_id = "subnet-0123456789abcdef0" }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { insecure_value = "ami-0123456789abcdef0" }
  }
}

variables {
  platform_instance_arn = "arn:aws:ec2:ap-northeast-2:111122223333:instance/i-0123456789abcdef0"
}

run "session_boundaries" {
  command = apply
  assert {
    condition     = one(aws_security_group.session.ingress).from_port == 2376 && one(one(aws_security_group.session.ingress).cidr_blocks) == "10.0.1.8/32"
    error_message = "Only the platform node may reach the Docker TLS port."
  }
  assert {
    condition     = !aws_instance.session.associate_public_ip_address && aws_instance.session.metadata_options[0].http_tokens == "required" && aws_instance.session.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "Private VM, IMDSv2 only, and containers cannot reach the instance role."
  }
  assert {
    condition     = strcontains(aws_instance.session.user_data, "\"tlsverify\": true") && !strcontains(aws_instance.session.user_data, "2375")
    error_message = "Docker listens only with client-certificate verification."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.tls.policy).Statement[0].Action == ["ssm:GetParameter"] && length(jsondecode(aws_iam_role_policy.tls.policy).Statement[0].Resource) == 3
    error_message = "The VM reads only its own three TLS parameters."
  }
  assert {
    condition     = output.docker_host == "tcp://${aws_instance.session.private_ip}:2376"
    error_message = "The broker's DOCKER_HOST points at the TLS port."
  }
}

run "other_region_rejected" {
  command = plan
  variables { platform_instance_arn = "arn:aws:ec2:us-east-1:111122223333:instance/i-0123456789abcdef0" }
  expect_failures = [var.platform_instance_arn]
}
