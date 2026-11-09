# Agents SDK sandbox session VM: Docker for the no-network proposal sandbox, separate from the
# platform node (no shared kernel with the control plane). The broker on the platform node is
# the only client: Docker listens on TLS 2376 with client-certificate verification (the
# authentication boundary), and the security group admits only the platform node's address.
# TLS keys never pass through Terraform: the operator writes them to SSM SecureString and the VM
# reads them at boot; without them dockerd does not start (fail closed).
terraform {
  required_version = ">= 1.16.5, < 2.0.0"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.71.0" }
  }
}

provider "aws" {
  region = "ap-northeast-2"
  default_tags { tags = { project = "bravo-platform", component = "agent-sandbox", "managed-by" = "terraform" } }
}

variable "platform_instance_arn" {
  description = "The platform RKE2 node that runs the subscription broker (the only Docker client)."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:ec2:ap-northeast-2:[0-9]{12}:instance/i-[a-f0-9]{17}$", var.platform_instance_arn))
    error_message = "Select the designated Seoul platform node."
  }
}
variable "instance_type" {
  description = "Session VM size; sandbox containers are short-lived and network-less."
  type        = string
  default     = "t3.medium"
}
variable "tls_parameter_prefix" {
  description = "SSM SecureString prefix holding ca.pem, server-cert.pem, server-key.pem (operator-written)."
  type        = string
  default     = "/bravo/agent-sandbox/docker-tls"
  validation {
    condition     = can(regex("^/bravo/agent-sandbox/[a-z0-9-]+$", var.tls_parameter_prefix))
    error_message = "Keep the TLS material under /bravo/agent-sandbox/."
  }
}

data "aws_instance" "platform" { instance_id = split("/", var.platform_instance_arn)[1] }
data "aws_subnet" "platform" { id = data.aws_instance.platform.subnet_id }
data "aws_caller_identity" "current" {}
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_iam_role" "session" {
  name = "bravo-agent-sandbox-session"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
  }] })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.session.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "tls" {
  name = "read-own-docker-tls-only"
  role = aws_iam_role.session.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["ssm:GetParameter"]
    Resource = [for name in ["ca.pem", "server-cert.pem", "server-key.pem"] :
    "arn:aws:ssm:ap-northeast-2:${data.aws_caller_identity.current.account_id}:parameter${var.tls_parameter_prefix}/${name}"]
  }] })
}
resource "aws_iam_instance_profile" "session" {
  name = "bravo-agent-sandbox-session"
  role = aws_iam_role.session.name
}

resource "aws_security_group" "session" {
  name_prefix = "bravo-agent-sandbox-"
  description = "Docker TLS from the platform node only; image pulls out"
  vpc_id      = data.aws_subnet.platform.vpc_id
  ingress {
    description = "Docker API (mTLS) from the broker platform node"
    protocol    = "tcp"
    from_port   = 2376
    to_port     = 2376
    cidr_blocks = ["${data.aws_instance.platform.private_ip}/32"]
  }
  egress {
    description = "dockerd image pulls and SSM; sandbox containers run with network none"
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "session" {
  ami                         = data.aws_ssm_parameter.al2023.insecure_value
  instance_type               = var.instance_type
  subnet_id                   = data.aws_instance.platform.subnet_id
  vpc_security_group_ids      = [aws_security_group.session.id]
  iam_instance_profile        = aws_iam_instance_profile.session.name
  associate_public_ip_address = false
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    prefix = var.tls_parameter_prefix
  })
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1 # containers cannot reach the instance role
  }
  root_block_device {
    encrypted   = true
    volume_type = "gp3"
    volume_size = 30
  }
  tags = { Name = "bravo-agent-sandbox-session" }
  lifecycle { ignore_changes = [ami] }
}

output "docker_host" { value = "tcp://${aws_instance.session.private_ip}:2376" }
output "instance_id" { value = aws_instance.session.id }
