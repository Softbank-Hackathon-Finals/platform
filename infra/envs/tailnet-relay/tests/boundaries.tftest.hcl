mock_provider "aws" {
  mock_data "aws_ec2_instance_type" {
    defaults = {
      supported_architectures = ["arm64"]
    }
  }
}
variables {
  account_id = "111122223333"
  ami_id     = "ami-0123456789abcdef0"
}

run "inbound_is_the_relay_port_only" {
  command = apply
  assert {
    condition     = aws_vpc_security_group_ingress_rule.relay.ip_protocol == "udp" && aws_vpc_security_group_ingress_rule.relay.from_port == 40000 && aws_vpc_security_group_ingress_rule.relay.to_port == 40000
    error_message = "The only inbound rule is the relay UDP port."
  }
  assert {
    condition     = length(aws_default_security_group.closed.ingress) == 0 && length(aws_default_security_group.closed.egress) == 0
    error_message = "The VPC default group stays closed."
  }
  assert {
    condition     = aws_instance.relay.key_name == "" && aws_instance.relay.metadata_options[0].http_tokens == "required" && aws_instance.relay.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "No SSH key pair; IMDSv2 with hop limit 1."
  }
  assert {
    condition     = length(aws_instance.relay.vpc_security_group_ids) == 1 && contains(aws_instance.relay.vpc_security_group_ids, aws_security_group.relay.id)
    error_message = "The host may only use the group this stack declares."
  }
  assert {
    condition     = strcontains(aws_instance.relay.user_data, "--relay-server-port='40000'") && !strcontains(aws_instance.relay.user_data, "auth-key")
    error_message = "The join starts the relay on the opened port and carries no auth key."
  }
}

run "tailscaled_port_is_refused" {
  command = plan
  variables {
    relay_port = 41641
  }
  expect_failures = [var.relay_port]
}

# A rule or policy added elsewhere would widen the boundary without failing the assertions above.
run "source_declares_only_reviewed_resources" {
  command = plan
  assert {
    condition = sort(flatten([for name in fileset(path.module, "*.tf") : [
      for match in regexall("(?m)^resource \"([a-z0-9_]+)\"", file("${path.module}/${name}")) : match[0]
      ]])) == sort([
      "aws_default_security_group",
      "aws_eip",
      "aws_iam_instance_profile",
      "aws_iam_role",
      "aws_iam_role_policy",
      "aws_iam_role_policy_attachment",
      "aws_instance",
      "aws_internet_gateway",
      "aws_route",
      "aws_route_table",
      "aws_route_table_association",
      "aws_security_group",
      "aws_subnet",
      "aws_vpc",
      "aws_vpc_security_group_egress_rule",
      "aws_vpc_security_group_egress_rule",
      "aws_vpc_security_group_ingress_rule",
    ])
    error_message = "A new resource changes the reviewed boundary; extend this list in the same change."
  }
}
