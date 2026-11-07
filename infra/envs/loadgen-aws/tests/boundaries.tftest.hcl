mock_provider "aws" {
  mock_data "aws_ec2_instance_type" {
    defaults = {
      default_vcpus                   = 2
      default_cores                   = 1
      default_threads_per_core        = 2
      memory_size                     = 2048
      supported_architectures         = ["x86_64"]
      burstable_performance_supported = true
    }
  }
}
variables {
  account_id    = "111122223333"
  ami_id        = "ami-0123456789abcdef0"
  tailscale_tag = "tag:bravo-loadgen"
}

run "no_inbound_and_https_only_outbound" {
  command = apply
  assert {
    condition     = length(aws_security_group.loadgen.ingress) == 0 && length(aws_default_security_group.closed.ingress) == 0 && length(aws_default_security_group.closed.egress) == 0
    error_message = "Neither the host security group nor the VPC default group may accept inbound traffic."
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.https.ip_protocol == "tcp" && aws_vpc_security_group_egress_rule.https.from_port == 443 && aws_vpc_security_group_egress_rule.https.to_port == 443
    error_message = "Outbound traffic is HTTPS only."
  }
  assert {
    condition     = aws_instance.loadgen.key_name == "" && aws_instance.loadgen.metadata_options[0].http_tokens == "required" && aws_instance.loadgen.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "No SSH key pair; IMDSv2 with hop limit 1."
  }
  assert {
    condition     = aws_route.internet.destination_cidr_block == "0.0.0.0/0" && aws_instance.loadgen.associate_public_ip_address == true && aws_subnet.loadgen.map_public_ip_on_launch == false
    error_message = "The host reaches the Edge directly through the internet gateway, and only this host gets a public address."
  }
  assert {
    condition     = length(aws_instance.loadgen.vpc_security_group_ids) == 1 && contains(aws_instance.loadgen.vpc_security_group_ids, aws_security_group.loadgen.id)
    error_message = "The host may only use the group whose rules the preconditions check."
  }
}

# The instance preconditions only see a separate rule resource on the plan after it is applied, and the test
# framework's mock teardown ignores prevent_destroy, so these are checked in the source.
run "source_declares_only_reviewed_resources" {
  command = plan
  # An extra security group, rule or IAM policy would widen the boundary without failing the checks on the
  # resources named below. Anyone adding a resource must extend this list, and the cost tag check with it.
  assert {
    condition = sort(flatten([for name in fileset(path.module, "*.tf") : [
      for match in regexall("(?m)^resource \"([a-z0-9_]+)\"", file("${path.module}/${name}")) : match[0]
      ]])) == sort([
      "aws_default_security_group",
      "aws_iam_instance_profile",
      "aws_iam_role",
      "aws_iam_role_policy",
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
    ])
    error_message = "This stack declares exactly the reviewed resources. Review the network and IAM boundary before adding one."
  }
  assert {
    condition = alltrue([for name in fileset(path.module, "*.tf") :
      !strcontains(file("${path.module}/${name}"), "prevent_destroy")
    ])
    error_message = "This stack must stay removable with terraform destroy after the demo."
  }
  assert {
    condition     = strcontains(file("${path.module}/main.tf"), "ignore_changes = [associate_public_ip_address]")
    error_message = "A stopped host has no public address; without this an apply would replace it and delete its results."
  }
}

run "ssm_only_role" {
  command = apply
  assert {
    condition     = length(jsondecode(aws_iam_role.loadgen.assume_role_policy).Statement) == 1 && keys(jsondecode(aws_iam_role.loadgen.assume_role_policy).Statement[0].Principal) == ["Service"] && jsondecode(aws_iam_role.loadgen.assume_role_policy).Statement[0].Principal.Service == "ec2.amazonaws.com" && jsondecode(aws_iam_role.loadgen.assume_role_policy).Statement[0].Action == "sts:AssumeRole"
    error_message = "Only this EC2 instance may assume the role."
  }
  assert {
    condition     = aws_iam_role_policy_attachment.ssm.policy_arn == "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    error_message = "The role gets the SSM core policy and nothing broader."
  }
  assert {
    condition = alltrue([for statement in jsondecode(aws_iam_role_policy.deny_credentials.policy).Statement : statement.Effect == "Deny"]) && alltrue([
      for action in ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "secretsmanager:GetSecretValue", "kms:Decrypt"] :
      contains(jsondecode(aws_iam_role_policy.deny_credentials.policy).Statement[0].Action, action)
    ])
    error_message = "The SSM core policy allows Parameter Store reads; the inline policy must only take credential reads away."
  }
}

run "no_sign_in_permission_by_default" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.sign_in) == 0
    error_message = "The host gets no Cognito permission unless the measured app requires sign-in."
  }
}

run "sign_in_permission_is_one_action_on_one_pool" {
  command = plan
  variables {
    source_user_pool_arn = "arn:aws:cognito-idp:ap-northeast-2:111122223333:userpool/ap-northeast-2_Demo123"
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.sign_in[0].policy).Statement == [{
      Effect   = "Allow"
      Action   = "cognito-idp:AdminInitiateAuth"
      Resource = "arn:aws:cognito-idp:ap-northeast-2:111122223333:userpool/ap-northeast-2_Demo123"
    }]
    error_message = "Exactly AdminInitiateAuth on the supplied pool: no other Cognito action, pool or wildcard."
  }
}

run "wildcard_pool_rejected" {
  command = plan
  variables {
    source_user_pool_arn = "arn:aws:cognito-idp:ap-northeast-2:111122223333:userpool/*"
  }
  expect_failures = [var.source_user_pool_arn]
}

run "floor_type_keeps_full_cpu_without_throttling" {
  command = apply
  assert {
    condition     = aws_instance.loadgen.instance_type == "t3.small"
    error_message = "The default is the smallest type at the floor."
  }
  assert {
    condition     = aws_instance.loadgen.credit_specification[0].cpu_credits == "unlimited"
    error_message = "T3 has no launch credits: standard mode would throttle the first measurement and report the generator as lagging."
  }
  assert {
    condition     = aws_instance.loadgen.cpu_options[0].core_count * aws_instance.loadgen.cpu_options[0].threads_per_core == 2
    error_message = "Request the selected type's full CPU capacity so a resize does not keep old CPU options."
  }
  assert {
    condition     = aws_instance.loadgen.root_block_device[0].encrypted && aws_instance.loadgen.root_block_device[0].delete_on_termination
    error_message = "The disk holds request journals: encrypt it and delete it with the host."
  }
}

run "resize_requests_the_new_types_full_cpu" {
  command = plan
  variables { instance_type = "t3.xlarge" }
  override_data {
    target = data.aws_ec2_instance_type.loadgen
    values = { default_vcpus = 4, default_cores = 2, default_threads_per_core = 2, memory_size = 16384, supported_architectures = ["x86_64"], burstable_performance_supported = true }
  }
  assert {
    condition     = aws_instance.loadgen.cpu_options[0].core_count * aws_instance.loadgen.cpu_options[0].threads_per_core == 4
    error_message = "CPU options must follow the selected type."
  }
}

run "fixed_performance_type_needs_no_credit_setting" {
  command = plan
  variables { instance_type = "m5.large" }
  override_data {
    target = data.aws_ec2_instance_type.loadgen
    values = { default_vcpus = 2, default_cores = 1, default_threads_per_core = 2, memory_size = 8192, supported_architectures = ["x86_64"], burstable_performance_supported = false }
  }
  assert {
    condition     = length(aws_instance.loadgen.credit_specification) == 0
    error_message = "CPU credits only exist on burstable types."
  }
}

run "every_taggable_resource_has_cost_tags" {
  command = plan
  assert {
    condition = alltrue([for tags in [
      aws_vpc.loadgen.tags,
      aws_default_security_group.closed.tags,
      aws_internet_gateway.loadgen.tags,
      aws_subnet.loadgen.tags,
      aws_route_table.loadgen.tags,
      aws_security_group.loadgen.tags,
      aws_vpc_security_group_egress_rule.https.tags,
      aws_iam_role.loadgen.tags,
      aws_iam_instance_profile.loadgen.tags,
      aws_instance.loadgen.tags,
      aws_instance.loadgen.root_block_device[0].tags,
    ] : tags.component == "platform" && tags.project == "bravo-platform"])
    error_message = "scripts/cost_report.py only attributes costs carrying project=bravo-platform and a contract component."
  }
}

run "user_data_installs_tools_but_holds_no_credentials" {
  command = plan
  assert {
    condition     = strcontains(aws_instance.loadgen.user_data, "--auth-key=file:/dev/stdin") && strcontains(aws_instance.loadgen.user_data, "--advertise-tags='tag:bravo-loadgen'")
    error_message = "The tailnet helper must read its key from stdin and join with the supplied tag."
  }
  assert {
    condition     = strcontains(aws_instance.loadgen.user_data, "--shields-up")
    error_message = "Security groups cannot see inside the tunnel, and the tailnet ACL lives outside this stack; the host refuses connections from peers itself."
  }
  assert {
    condition     = !strcontains(aws_instance.loadgen.user_data, "tskey") && !strcontains(aws_instance.loadgen.user_data, "postgres") && !strcontains(aws_instance.loadgen.user_data, "_DSN") && !strcontains(aws_instance.loadgen.user_data, "BRAVO_WINDOW_TOKEN")
    error_message = "User data stays readable on the host and in state; credentials must arrive through an operator session."
  }
  assert {
    condition     = strcontains(aws_instance.loadgen.user_data, "uv==0.9.5") && strcontains(aws_instance.loadgen.user_data, "tailscale=1.104.4") && strcontains(aws_instance.loadgen.user_data, "sha256sum -c -")
    error_message = "Pin the tools and verify the package signing key."
  }
  assert {
    condition     = !strcontains(aws_instance.loadgen.user_data, "umask 077")
    error_message = "The non-root SSM user must be able to run the installed tools."
  }
}

# state_key gives these two runs an empty state: overrides only reach a resource that is being created,
# so state left by the apply runs above would hide the rule and the run would prove nothing.
run "inbound_rule_rejected" {
  command   = plan
  state_key = "rule_overrides"
  override_resource {
    target          = aws_security_group.loadgen
    override_during = plan
    values = {
      ingress = [{ protocol = "tcp", from_port = 22, to_port = 22, cidr_blocks = ["0.0.0.0/0"], description = "", ipv6_cidr_blocks = [], prefix_list_ids = [], security_groups = [], self = false }]
    }
  }
  expect_failures = [aws_instance.loadgen]
}

run "non_https_outbound_rule_rejected" {
  command   = plan
  state_key = "rule_overrides"
  override_resource {
    target          = aws_security_group.loadgen
    override_during = plan
    values = {
      egress = [{ protocol = "-1", from_port = 0, to_port = 0, cidr_blocks = ["0.0.0.0/0"], description = "", ipv6_cidr_blocks = [], prefix_list_ids = [], security_groups = [], self = false }]
    }
  }
  expect_failures = [aws_instance.loadgen]
}

run "one_gib_type_rejected" {
  command = plan
  variables { instance_type = "t3.micro" }
  override_data {
    target = data.aws_ec2_instance_type.loadgen
    values = { default_vcpus = 2, default_cores = 1, default_threads_per_core = 2, memory_size = 1024, supported_architectures = ["x86_64"], burstable_performance_supported = true }
  }
  expect_failures = [aws_instance.loadgen]
}

run "one_vcpu_type_rejected" {
  command = plan
  variables { instance_type = "t2.small" }
  override_data {
    target = data.aws_ec2_instance_type.loadgen
    values = { default_vcpus = 1, default_cores = 1, default_threads_per_core = 1, memory_size = 2048, supported_architectures = ["i386", "x86_64"], burstable_performance_supported = true }
  }
  expect_failures = [aws_instance.loadgen]
}

run "other_architecture_rejected" {
  command = plan
  variables { instance_type = "t4g.small" }
  override_data {
    target = data.aws_ec2_instance_type.loadgen
    values = { default_vcpus = 2, default_cores = 2, default_threads_per_core = 1, memory_size = 2048, supported_architectures = ["arm64"], burstable_performance_supported = true }
  }
  expect_failures = [aws_instance.loadgen]
}

run "missing_component_tag_rejected" {
  command = plan
  variables { tags = { project = "bravo-platform" } }
  expect_failures = [var.tags]
}

run "missing_project_tag_rejected" {
  command = plan
  variables { tags = { component = "platform" } }
  expect_failures = [var.tags]
}

run "component_outside_cost_contract_rejected" {
  command = plan
  variables { tags = { project = "bravo-platform", component = "loadgen" } }
  expect_failures = [var.tags]
}

run "tag_without_prefix_rejected" {
  command = plan
  variables { tailscale_tag = "bravo-loadgen" }
  expect_failures = [var.tailscale_tag]
}

run "auth_key_in_tag_input_rejected" {
  command = plan
  variables { tailscale_tag = "tskey-auth-EXAMPLE" }
  expect_failures = [var.tailscale_tag]
}

run "malformed_account_rejected" {
  command = plan
  variables { account_id = "1234" }
  expect_failures = [var.account_id]
}

run "malformed_ami_rejected" {
  command = plan
  variables { ami_id = "ubuntu-24.04" }
  expect_failures = [var.ami_id]
}
