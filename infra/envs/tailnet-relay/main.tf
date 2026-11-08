data "aws_ec2_instance_type" "relay" { instance_type = var.instance_type }

locals {
  # Mock providers do not merge default_tags, so resources also set tags explicitly to keep them testable.
  named_tags = merge(var.tags, { Name = var.name })
}

# Peers reach the relay over the internet (the on-prem VM, AWS private subnets through their NAT gateways),
# so it needs no peering and stays out of the VPCs whose routes it serves.
resource "aws_vpc" "relay" {
  cidr_block           = "10.94.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = local.named_tags
}
resource "aws_default_security_group" "closed" {
  vpc_id  = aws_vpc.relay.id
  ingress = []
  egress  = []
  tags    = local.named_tags
}
resource "aws_internet_gateway" "relay" {
  vpc_id = aws_vpc.relay.id
  tags   = local.named_tags
}
resource "aws_subnet" "relay" {
  vpc_id                  = aws_vpc.relay.id
  cidr_block              = "10.94.0.0/27"
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = false
  tags                    = local.named_tags
}
resource "aws_route_table" "relay" {
  vpc_id = aws_vpc.relay.id
  tags   = local.named_tags
}
resource "aws_route" "internet" {
  route_table_id         = aws_route_table.relay.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.relay.id
}
resource "aws_route_table_association" "relay" {
  subnet_id      = aws_subnet.relay.id
  route_table_id = aws_route_table.relay.id
}

resource "aws_security_group" "relay" {
  name        = var.name
  description = "Tailscale peer relay: inbound only on the relay UDP port; operators use SSM"
  vpc_id      = aws_vpc.relay.id
  tags        = local.named_tags
}
# Who may relay is decided by the tailnet grant (tailscale.com/cap/relay); packets without a relay session are dropped.
resource "aws_vpc_security_group_ingress_rule" "relay" {
  security_group_id = aws_security_group.relay.id
  description       = "Peer relay UDP port; peers behind hard NAT send relayed packets here"
  ip_protocol       = "udp"
  from_port         = var.relay_port
  to_port           = var.relay_port
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.named_tags
}
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.relay.id
  description       = "SSM, package installs, and Tailscale control and DERP"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.named_tags
}
resource "aws_vpc_security_group_egress_rule" "udp" {
  security_group_id = aws_security_group.relay.id
  description       = "Relayed packets to peers' NAT ports, STUN, and WireGuard"
  ip_protocol       = "udp"
  from_port         = 1
  to_port           = 65535
  cidr_ipv4         = "0.0.0.0/0"
  tags              = local.named_tags
}

resource "aws_iam_role" "relay" {
  name = var.name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
  }] })
  tags = local.named_tags
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.relay.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
# The SSM core policy includes Parameter Store reads; this host holds no credentials at all.
resource "aws_iam_role_policy" "deny_credentials" {
  name = "no-runtime-secrets"
  role = aws_iam_role.relay.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Deny"
    Action = [
      "ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:GetParameterHistory",
      "secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue", "kms:Decrypt",
    ]
    Resource = "*"
  }] })
}
resource "aws_iam_instance_profile" "relay" {
  name = var.name
  role = aws_iam_role.relay.name
  tags = local.named_tags
}

# An Elastic IP keeps the advertised relay address across stop/start.
resource "aws_eip" "relay" {
  domain   = "vpc"
  instance = aws_instance.relay.id
  tags     = local.named_tags
}

resource "aws_instance" "relay" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  key_name                    = ""
  subnet_id                   = aws_subnet.relay.id
  associate_public_ip_address = false # the Elastic IP is the host's only public address
  vpc_security_group_ids      = [aws_security_group.relay.id]
  iam_instance_profile        = aws_iam_instance_profile.relay.name
  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    hostname      = var.name
    tailscale_tag = var.tailscale_tag
    relay_port    = var.relay_port
  })
  user_data_replace_on_change = true
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_size           = 8
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
    tags                  = local.named_tags
  }
  tags = local.named_tags
  depends_on = [
    aws_route.internet,
    aws_route_table_association.relay,
    aws_iam_role_policy_attachment.ssm,
    aws_iam_role_policy.deny_credentials,
    aws_vpc_security_group_egress_rule.https,
    aws_vpc_security_group_egress_rule.udp,
  ]
  lifecycle {
    precondition {
      condition     = contains(data.aws_ec2_instance_type.relay.supported_architectures, "arm64")
      error_message = "The relay host image is arm64."
    }
  }
}

output "operator" {
  sensitive = true
  value = {
    instance_id   = aws_instance.relay.id
    public_ip     = aws_eip.relay.public_ip
    relay_port    = var.relay_port
    tailscale_tag = var.tailscale_tag
  }
}
