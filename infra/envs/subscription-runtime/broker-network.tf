# Leave the VPC default STS route unchanged; only the broker uses the private endpoint.
data "aws_instance" "broker" {
  instance_id = split("/", var.instance_arn)[1]
  lifecycle {
    postcondition {
      condition = try(
        contains(self.secondary_private_ips, self.tags["bravo:subscription-sts-ipv4"]) &&
        self.tags["bravo:subscription-sts-ipv4"] != self.private_ip, false
      )
      error_message = "The platform node must designate an assigned secondary IPv4 for broker STS."
    }
  }
}

data "aws_subnet" "broker" {
  id = data.aws_instance.broker.subnet_id
}

resource "aws_security_group" "broker_sts" {
  name_prefix = "bravo-broker-sts-"
  description = "Regional STS for the designated subscription VM"
  vpc_id      = data.aws_subnet.broker.vpc_id
  ingress {
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = ["${local.broker_source_ip}/32"]
  }
}

resource "aws_vpc_endpoint" "broker_sts" {
  vpc_id              = data.aws_subnet.broker.vpc_id
  subnet_ids          = [data.aws_instance.broker.subnet_id]
  security_group_ids  = [aws_security_group.broker_sts.id]
  vpc_endpoint_type   = "Interface"
  service_name        = "com.amazonaws.ap-northeast-2.sts"
  private_dns_enabled = false
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Principal = "*", Action = "sts:AssumeRoleWithWebIdentity"
    Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${local.broker_name}"
  }] })
  tags = { Name = "bravo-subscription-broker-sts" }
}

locals {
  broker_source_ip = data.aws_instance.broker.tags["bravo:subscription-sts-ipv4"]

  # Exclude the zonal DNS entry. Endpoints are discovered from the provider, never from a run request.
  broker_sts_dns = one([for entry in aws_vpc_endpoint.broker_sts.dns_entry : entry.dns_name
    if can(regex("^vpce-[a-f0-9]+-[a-z0-9]+\\.sts\\.ap-northeast-2\\.vpce\\.amazonaws\\.com$", entry.dns_name))
  ])
}

data "aws_network_interface" "broker_sts" {
  id = one(aws_vpc_endpoint.broker_sts.network_interface_ids)
}

resource "aws_ssm_association" "broker_sts" {
  name                             = "AWS-RunShellScript"
  association_name                 = "bravo-subscription-broker-sts"
  wait_for_success_timeout_seconds = 180
  targets {
    key    = "InstanceIds"
    values = [data.aws_instance.broker.id]
  }
  parameters = {
    commands = templatefile("${path.module}/broker-guard.sh.tftpl", {
      broker_source_ip = local.broker_source_ip
      endpoint_dns     = local.broker_sts_dns
      endpoint_ip      = data.aws_network_interface.broker_sts.private_ip
    })
  }
  lifecycle {
    precondition {
      condition     = local.broker_sts_dns != null
      error_message = "A regional STS interface endpoint DNS name is required."
    }
  }
}
