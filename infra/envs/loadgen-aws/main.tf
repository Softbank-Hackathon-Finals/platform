data "aws_ec2_instance_type" "loadgen" { instance_type = var.instance_type }

locals {
  # Mock providers do not merge default_tags, so resources also set tags explicitly to keep them testable.
  named_tags = merge(var.tags, { Name = var.name })
}

# Judgement inputs are measured from outside the evaluated systems (ADR-0010), so this VPC peers with nothing.
resource "aws_vpc" "loadgen" {
  cidr_block           = "10.93.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = local.named_tags
}
resource "aws_default_security_group" "closed" {
  vpc_id  = aws_vpc.loadgen.id
  ingress = []
  egress  = []
  tags    = local.named_tags
}
resource "aws_internet_gateway" "loadgen" {
  vpc_id = aws_vpc.loadgen.id
  tags   = local.named_tags
}
resource "aws_subnet" "loadgen" {
  vpc_id                  = aws_vpc.loadgen.id
  cidr_block              = "10.93.0.0/27"
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = false
  tags                    = local.named_tags
}
resource "aws_route_table" "loadgen" {
  vpc_id = aws_vpc.loadgen.id
  tags   = local.named_tags
}
resource "aws_route" "internet" {
  route_table_id         = aws_route_table.loadgen.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.loadgen.id
}
resource "aws_route_table_association" "loadgen" {
  subnet_id      = aws_subnet.loadgen.id
  route_table_id = aws_route_table.loadgen.id
}

# ingress stays unset on purpose: AWS then reports real rules here, and the instance preconditions check them.
resource "aws_security_group" "loadgen" {
  name        = var.name
  description = "Load generator: no ingress; operators use authenticated SSM sessions"
  vpc_id      = aws_vpc.loadgen.id
  tags        = local.named_tags
}
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.loadgen.id
  description       = "Cloudflare Edge, SSM, package installs, and Tailscale control and DERP relays"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.named_tags
}

resource "aws_iam_role" "loadgen" {
  name = var.name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
  }] })
  tags = local.named_tags
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.loadgen.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
# The SSM core policy includes Parameter Store reads; this host receives credentials only from an operator session.
resource "aws_iam_role_policy" "deny_credentials" {
  name = "no-runtime-secrets"
  role = aws_iam_role.loadgen.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Deny"
    Action = [
      "ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:GetParameterHistory",
      "secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue", "kms:Decrypt",
    ]
    Resource = "*"
  }] })
}
# Only when the measured app requires sign-in: AdminInitiateAuth on the source user pool, nothing else.
# IAM narrows this to the pool; within it, only source-identity's load generator client accepts the admin flow,
# so the same permission yields no token through the browser client.
resource "aws_iam_role_policy" "sign_in" {
  count = var.source_user_pool_arn == null ? 0 : 1
  name  = "loadgen-sign-in"
  role  = aws_iam_role.loadgen.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = "cognito-idp:AdminInitiateAuth"
    Resource = var.source_user_pool_arn
  }] })
}
resource "aws_iam_instance_profile" "loadgen" {
  name = var.name
  role = aws_iam_role.loadgen.name
  tags = local.named_tags
}

resource "aws_instance" "loadgen" {
  ami           = var.ami_id
  instance_type = var.instance_type
  cpu_options {
    core_count       = data.aws_ec2_instance_type.loadgen.default_cores
    threads_per_core = data.aws_ec2_instance_type.loadgen.default_threads_per_core
  }
  key_name                    = ""
  subnet_id                   = aws_subnet.loadgen.id
  associate_public_ip_address = true # Outbound only; cheaper than a NAT gateway, and the security group has no ingress.
  vpc_security_group_ids      = [aws_security_group.loadgen.id]
  iam_instance_profile        = aws_iam_instance_profile.loadgen.name
  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    hostname      = var.name
    tailscale_tag = var.tailscale_tag
  })
  user_data_replace_on_change = true
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_size           = 20
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
    tags                  = local.named_tags
  }
  # Throttling here would look like a slow target. Set it explicitly because an account can change the T3 default.
  dynamic "credit_specification" {
    for_each = data.aws_ec2_instance_type.loadgen.burstable_performance_supported ? [1] : []
    content {
      cpu_credits = "unlimited"
    }
  }
  tags = local.named_tags
  depends_on = [
    aws_route.internet,
    aws_route_table_association.loadgen,
    aws_iam_role_policy_attachment.ssm,
    aws_iam_role_policy.deny_credentials,
    aws_vpc_security_group_egress_rule.https,
  ]
  lifecycle {
    # Stopping releases the auto-assigned address. The provider then reads false for this force-new attribute,
    # so without this an apply between measurements would replace the host and delete its results.
    ignore_changes = [associate_public_ip_address]
    precondition {
      condition     = data.aws_ec2_instance_type.loadgen.default_vcpus >= 2 && data.aws_ec2_instance_type.loadgen.memory_size >= 2048
      error_message = "The load generator requires at least 2 vCPUs and 2 GiB; see README.md for the basis."
    }
    precondition {
      condition     = contains(data.aws_ec2_instance_type.loadgen.supported_architectures, "x86_64")
      error_message = "The load generator host must be x86_64."
    }
    precondition {
      condition     = length(aws_security_group.loadgen.ingress) == 0
      error_message = "The host accepts no inbound traffic. Remove the rule; operators use SSM."
    }
    precondition {
      condition = alltrue([for rule in aws_security_group.loadgen.egress :
        rule.protocol == "tcp" && rule.from_port == 443 && rule.to_port == 443
      ])
      error_message = "The host only sends HTTPS. Opening another outbound port is a reviewed change to this stack."
    }
  }
}

output "operator" {
  sensitive = true
  value     = { instance_id = aws_instance.loadgen.id, tailscale_tag = var.tailscale_tag }
}
