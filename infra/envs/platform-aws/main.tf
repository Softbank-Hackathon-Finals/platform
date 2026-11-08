data "aws_subnet" "private" { id = var.private_subnet_id }
data "aws_vpc" "foundation" { id = var.vpc_id }
data "aws_ec2_instance_type" "node" { instance_type = var.instance_type }

# This stack owns its egress route; it does not rewrite T1 peering routes.
resource "aws_internet_gateway" "egress" { vpc_id = var.vpc_id }
resource "aws_subnet" "egress" {
  vpc_id                  = var.vpc_id
  cidr_block              = var.egress_subnet_cidr
  availability_zone       = data.aws_subnet.private.availability_zone
  map_public_ip_on_launch = false
}
resource "aws_route_table" "egress" { vpc_id = var.vpc_id }
resource "aws_route" "internet" {
  route_table_id         = aws_route_table.egress.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.egress.id
}
resource "aws_route_table_association" "egress" {
  subnet_id      = aws_subnet.egress.id
  route_table_id = aws_route_table.egress.id
}
resource "aws_eip" "egress" { domain = "vpc" }
resource "aws_nat_gateway" "egress" {
  allocation_id = aws_eip.egress.id
  subnet_id     = aws_subnet.egress.id
  depends_on    = [aws_route.internet]
}
resource "aws_route" "private_egress" {
  route_table_id         = var.private_route_table_id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.egress.id
}

resource "aws_security_group" "node" {
  name        = var.name
  description = "Private RKE2: no ingress; operator uses authenticated SSM sessions"
  vpc_id      = var.vpc_id
}
# HTTPS release/registry/SSM endpoints have changing addresses. No inbound rule or public node IP.
# trivy:ignore:AVD-AWS-0104
resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.node.id
  description       = "Outbound HTTPS for pinned packages, registries and SSM; pod egress is a separate T3 boundary"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}
# Argo CD UI tunnel (infra/envs/argocd-exposure): cloudflared dials only Cloudflare's tunnel edge on 7844.
# Ranges are Cloudflare's published tunnel edge (region1/region2); nothing else opens beyond HTTPS.
resource "aws_vpc_security_group_egress_rule" "cloudflare_tunnel" {
  for_each          = toset(["198.41.192.0/24", "198.41.200.0/24"])
  security_group_id = aws_security_group.node.id
  description       = "Outbound Cloudflare Tunnel edge (Argo CD UI connector)"
  ip_protocol       = "tcp"
  from_port         = 7844
  to_port           = 7844
  cidr_ipv4         = each.value
}
resource "aws_iam_role" "node" {
  name = var.name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
  }] })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "no_secrets" {
  name = "no-runtime-secrets"
  role = aws_iam_role.node.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Deny", Action = ["secretsmanager:GetSecretValue", "ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "kms:Decrypt"], Resource = "*"
  }] })
}
resource "aws_iam_instance_profile" "node" {
  name = var.name
  role = aws_iam_role.node.name
}
resource "aws_instance" "node" {
  tags = merge({ Name = var.name }, var.subscription_broker_ipv4 == null ? {} : {
    "bravo:subscription-sts-ipv4" = var.subscription_broker_ipv4
  })
  ami           = var.ami_id
  instance_type = var.instance_type
  cpu_options {
    core_count       = data.aws_ec2_instance_type.node.default_cores
    threads_per_core = data.aws_ec2_instance_type.node.default_threads_per_core
  }
  secondary_private_ips       = var.subscription_broker_ipv4 == null ? null : [var.subscription_broker_ipv4]
  subnet_id                   = var.private_subnet_id
  associate_public_ip_address = false
  vpc_security_group_ids      = [aws_security_group.node.id]
  iam_instance_profile        = aws_iam_instance_profile.node.name
  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    rke2_bootstrap = file("${path.module}/../../shared/rke2/bootstrap.sh")
    rke2_version   = var.rke2_version
  })
  user_data_replace_on_change = true
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_size           = 40
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
    tags                  = { project = "bravo-platform", component = "platform", "managed-by" = "terraform" }
  }
  credit_specification { cpu_credits = "standard" }
  depends_on = [aws_route.private_egress, aws_iam_role_policy_attachment.ssm]
  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        data.aws_ec2_instance_type.node.default_vcpus >= 4 &&
        data.aws_ec2_instance_type.node.memory_size >= 8192 &&
        contains(data.aws_ec2_instance_type.node.supported_architectures, "x86_64")
      )
      error_message = "The single platform server requires at least 4 vCPUs, 8 GiB and x86_64; do not shrink workload requests to fit an undersized node."
    }
    precondition {
      condition     = data.aws_subnet.private.vpc_id == var.vpc_id && data.aws_subnet.private.map_public_ip_on_launch == false
      error_message = "RKE2 must use the reviewed private subnet in the platform VPC."
    }
  }
}

output "operator" {
  sensitive = true
  value     = { instance_id = aws_instance.node.id, role_arn = aws_iam_role.actions.arn, document_name = aws_ssm_document.smoke.name }
}
