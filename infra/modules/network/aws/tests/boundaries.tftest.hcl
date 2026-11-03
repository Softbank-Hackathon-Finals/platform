mock_provider "aws" {}

variables { account_id = "123456789012" }

run "private_foundation" {
  command = plan
  assert {
    condition = length(aws_vpc.environment) == 2 && length(aws_subnet.private) == 4 && alltrue([
      for subnet in aws_subnet.private : subnet.map_public_ip_on_launch == false
    ])
    error_message = "Both environments need two private subnets without automatic public IPs."
  }
  assert {
    condition = alltrue([
      for group in aws_default_security_group.closed : length(group.ingress) == 0 && length(group.egress) == 0
    ])
    error_message = "Default security groups must not allow traffic."
  }
  assert {
    condition = alltrue([
      for route in aws_route.peer : route.destination_cidr_block != "0.0.0.0/0"
    ]) && length(aws_route.peer) == 2
    error_message = "Only reciprocal private routes are part of this foundation."
  }
  assert {
    condition = alltrue([
      for rule in aws_vpc_security_group_ingress_rule.source : rule.cidr_ipv4 == null && rule.cidr_ipv6 == null && rule.from_port == rule.to_port
    ]) && toset([for rule in aws_vpc_security_group_ingress_rule.source : rule.to_port]) == toset([443, 5432])
    error_message = "Source ingress must use peer group references on exactly HTTPS and PostgreSQL."
  }
  assert {
    condition = alltrue([
      for log in aws_flow_log.vpc : log.traffic_type == "ALL"
    ]) && aws_kms_key.flow_logs.enable_key_rotation
    error_message = "Both allowed and denied traffic need encrypted flow logs."
  }
}

run "reject_duplicate_zones" {
  command = plan
  variables { availability_zones = ["ap-northeast-2a", "ap-northeast-2a"] }
  expect_failures = [var.availability_zones]
}

run "reject_wrong_region" {
  command = plan
  variables { availability_zones = ["us-east-1a", "us-east-1b"] }
  expect_failures = [var.availability_zones]
}

run "reject_non_private_range" {
  command = plan
  variables { network_cidr = "8.80.0.0/15" }
  expect_failures = [var.network_cidr]
}

run "reject_unaligned_range" {
  command = plan
  variables { network_cidr = "10.81.0.0/15" }
  expect_failures = [var.network_cidr]
}

run "reject_bad_account" {
  command = plan
  variables { account_id = "1234" }
  expect_failures = [var.account_id]
}
