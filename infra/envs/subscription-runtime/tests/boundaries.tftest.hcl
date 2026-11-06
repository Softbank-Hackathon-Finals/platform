mock_provider "aws" {
  mock_data "aws_instance" {
    defaults = { private_ip = "10.0.1.8", secondary_private_ips = ["10.0.1.9"], tags = { "bravo:subscription-sts-ipv4" = "10.0.1.9" }, subnet_id = "subnet-0123456789abcdef0", iam_instance_profile = "bravo-platform-node" }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0" }
  }
  mock_data "aws_network_interface" {
    defaults = { private_ip = "10.0.1.12" }
  }
  mock_resource "aws_vpc_endpoint" {
    defaults = {
      id                    = "vpce-0123456789abcdef0"
      network_interface_ids = ["eni-0123456789abcdef0"]
      dns_entry = [
        { dns_name = "vpce-0123456789abcdef0-abc123.sts.ap-northeast-2.vpce.amazonaws.com", hosted_zone_id = "ZEXAMPLE" },
        { dns_name = "vpce-0123456789abcdef0-abc123-ap-northeast-2a.sts.ap-northeast-2.vpce.amazonaws.com", hosted_zone_id = "ZEXAMPLE" }
      ]
    }
  }
  mock_data "aws_iam_instance_profile" {
    defaults = { role_arn = "arn:aws:iam::111122223333:role/bravo-platform-node" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
}

variables {
  instance_arn            = "arn:aws:ec2:ap-northeast-2:111122223333:instance/i-0123456789abcdef0"
  subscription_secret_arn = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:bravo-platform/agent-subscription/example-ABC123"
}

run "credential_and_session_boundaries" {
  command = apply
  assert {
    condition = one(one(aws_security_group.broker_sts.ingress).cidr_blocks) == "10.0.1.9/32" && strcontains(
      aws_ssm_association.broker_sts.parameters["commands"], "snat ip to 10.0.1.9"
    )
    error_message = "AWS accepts only the secondary address installed by broker-only SNAT."
  }
  assert {
    condition = alltrue([for grant in jsondecode(aws_iam_role.broker.assume_role_policy).Statement :
      grant.Condition.StringEquals["aws:SourceVpce"] == aws_vpc_endpoint.broker_sts.id &&
      grant.Condition.IpAddress["aws:VpcSourceIp"] == "10.0.1.9/32"
    ])
    error_message = "Each broker grant must require the endpoint and the designated VM address."
  }
  assert {
    condition = length([for grant in jsondecode(aws_iam_role.broker.assume_role_policy).Statement : grant
      if grant.Action == "sts:AssumeRole" && grant.Principal.AWS == "arn:aws:iam::111122223333:role/bravo-platform-node"
    ]) == 1 && length(jsondecode(aws_iam_role.broker.assume_role_policy).Statement) == 1
    error_message = "The broker trusts only the node role (platform Job path); no GitHub Actions grant."
  }
  assert {
    condition = [for s in jsondecode(aws_vpc_endpoint.broker_sts.policy).Statement : s.Action] == ["sts:AssumeRole"]
    error_message = "The broker endpoint must allow the same AssumeRole the broker trust grants."
  }
  assert {
    condition = !aws_vpc_endpoint.broker_sts.private_dns_enabled && strcontains(
      aws_ssm_association.broker_sts.parameters["commands"], "AWS_ENDPOINT_URL_STS=https://vpce-0123456789abcdef0-abc123.sts."
    )
    error_message = "Configure private STS only for the broker."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.broker.policy).Statement[0].Resource == var.subscription_secret_arn && length(jsondecode(aws_iam_role_policy.broker.policy).Statement[0].Action) == 3
    error_message = "The VM broker only reads and rotates its exact subscription Secret."
  }
  assert {
    condition     = jsondecode(aws_ssm_document.tunnel.content).properties.portNumber == "18761" && jsondecode(aws_ssm_document.tunnel.content).sessionType == "Port"
    error_message = "The tunnel must target the fixed loopback broker port."
  }
  assert {
    condition     = can(regex(jsondecode(aws_ssm_document.service.content).parameters.runId.allowedPattern, "subscription-0123456789abcdef0123456789abcdef")) && !can(regex(jsondecode(aws_ssm_document.service.content).parameters.runId.allowedPattern, "../../proposal")) && !can(regex(jsondecode(aws_ssm_document.service.content).parameters.proposalDigest.allowedPattern, "not-a-digest"))
    error_message = "Verification accepts only bounded execution IDs and proposal digests, never paths or commands."
  }
}
run "wildcard_secret_rejected" {
  command = plan
  variables { subscription_secret_arn = "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:*" }
  expect_failures = [var.subscription_secret_arn]
}
run "other_instance_region_rejected" {
  command = plan
  variables { instance_arn = "arn:aws:ec2:us-east-1:111122223333:instance/i-0123456789abcdef0" }
  expect_failures = [var.instance_arn]
}

run "node_without_broker_source_rejected" {
  command = plan
  override_data {
    target = data.aws_instance.broker
    values = { private_ip = "10.0.1.8", secondary_private_ips = [], tags = {}, subnet_id = "subnet-0123456789abcdef0", iam_instance_profile = "bravo-platform-node" }
  }
  expect_failures = [data.aws_instance.broker]
}

run "primary_node_address_is_never_a_broker_identity" {
  command = plan
  override_data {
    target = data.aws_instance.broker
    values = {
      private_ip            = "10.0.1.8"
      secondary_private_ips = ["10.0.1.9"]
      tags                  = { "bravo:subscription-sts-ipv4" = "10.0.1.8" }
      subnet_id             = "subnet-0123456789abcdef0"
    }
  }
  expect_failures = [data.aws_instance.broker]
}
