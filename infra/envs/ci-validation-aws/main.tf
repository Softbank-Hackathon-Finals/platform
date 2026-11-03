terraform {
  required_version = ">= 1.16.5, < 2.0.0"
  backend "local" {}
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.71.0"
    }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      project      = "bravo-platform"
      component    = "platform"
      purpose      = "ci-validation"
      deployment   = var.deployment
      expires_at   = var.expires_at
      "managed-by" = "terraform"
    }
  }
}

locals {
  name = "bravo-ci-${var.deployment}"
}

# This disposable network never attaches to the source/platform foundation.
resource "aws_vpc" "validation" {
  cidr_block           = "10.92.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_default_security_group" "closed" {
  vpc_id  = aws_vpc.validation.id
  ingress = []
  egress  = []
}

resource "aws_internet_gateway" "validation" {
  vpc_id = aws_vpc.validation.id
}

resource "aws_subnet" "validation" {
  vpc_id                  = aws_vpc.validation.id
  cidr_block              = "10.92.0.0/27"
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = false
}

resource "aws_route_table" "validation" {
  vpc_id = aws_vpc.validation.id
}

resource "aws_route" "bootstrap" {
  route_table_id         = aws_route_table.validation.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.validation.id
}

resource "aws_route_table_association" "validation" {
  subnet_id      = aws_subnet.validation.id
  route_table_id = aws_route_table.validation.id
}

resource "aws_security_group" "host" {
  name        = local.name
  description = "SSM-only validation host: no inbound, HTTPS bootstrap and VPC DNS outbound"
  vpc_id      = aws_vpc.validation.id
  ingress     = []
}

resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.host.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
  description       = "Host package downloads and SSM; sandbox egress is separately denied by NetworkPolicy"
}

resource "aws_vpc_security_group_egress_rule" "dns" {
  for_each          = toset(["tcp", "udp"])
  security_group_id = aws_security_group.host.id
  ip_protocol       = each.value
  from_port         = 53
  to_port           = 53
  cidr_ipv4         = "10.92.0.2/32"
  description       = "VPC resolver only"
}

resource "aws_iam_role" "ssm" {
  name = local.name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Managed SSM core includes GetParameter(s); this host has no reason to read secrets.
resource "aws_iam_role_policy" "deny_credentials" {
  name = "deny-application-and-model-credentials"
  role = aws_iam_role.ssm.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Deny"
      Action = [
        "ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:GetParameterHistory",
        "secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue", "kms:Decrypt",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_iam_instance_profile" "ssm" {
  name = local.name
  role = aws_iam_role.ssm.name
}

resource "aws_instance" "validation" {
  ami                                  = var.ami_id
  instance_type                        = "t4g.medium"
  key_name                             = ""
  subnet_id                            = aws_subnet.validation.id
  associate_public_ip_address          = true
  vpc_security_group_ids               = [aws_security_group.host.id]
  iam_instance_profile                 = aws_iam_instance_profile.ssm.name
  instance_initiated_shutdown_behavior = "terminate"
  user_data_replace_on_change          = true
  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    ttl_minutes = var.ttl_minutes
  })
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }
  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = 20
    delete_on_termination = true
    tags = {
      project      = "bravo-platform"
      component    = "platform"
      purpose      = "ci-validation"
      deployment   = var.deployment
      expires_at   = var.expires_at
      "managed-by" = "terraform"
    }
  }
  credit_specification {
    cpu_credits = "standard"
  }
  tags = { Name = local.name }
  depends_on = [
    aws_route.bootstrap,
    aws_route_table_association.validation,
    aws_iam_role_policy_attachment.ssm,
    aws_iam_role_policy.deny_credentials,
    aws_vpc_security_group_egress_rule.https,
  ]
}

output "validation" {
  value = {
    instance_id = aws_instance.validation.id
    vpc_id      = aws_vpc.validation.id
    role_name   = aws_iam_role.ssm.name
    region      = var.region
    deployment  = var.deployment
    ami_id      = var.ami_id
  }
}
