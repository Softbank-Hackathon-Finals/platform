locals {
  environments = {
    source   = cidrsubnet(var.network_cidr, 1, 0)
    platform = cidrsubnet(var.network_cidr, 1, 1)
  }
  subnets = merge([
    for env, cidr in local.environments : {
      for index, az in var.availability_zones : "${env}-${index}" => {
        env  = env
        az   = az
        cidr = cidrsubnet(cidr, 8, index)
      }
    }
  ]...)
}

resource "aws_vpc" "environment" {
  for_each             = local.environments
  cidr_block           = each.value
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.name}-${each.key}", environment = each.key, component = each.key }
}

resource "aws_default_security_group" "closed" {
  for_each = local.environments
  vpc_id   = aws_vpc.environment[each.key].id
  # Empty rules also remove AWS's default outbound allow-all rule.
  ingress = []
  egress  = []
  tags    = { Name = "${var.name}-${each.key}-default-closed", environment = each.key, component = each.key }
}

resource "aws_subnet" "private" {
  for_each                = local.subnets
  vpc_id                  = aws_vpc.environment[each.value.env].id
  availability_zone       = each.value.az
  cidr_block              = each.value.cidr
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.name}-${each.key}", environment = each.value.env, component = each.value.env }
}

resource "aws_route_table" "private" {
  for_each = local.environments
  vpc_id   = aws_vpc.environment[each.key].id
  tags     = { Name = "${var.name}-${each.key}-private", environment = each.key, component = each.key }
}

resource "aws_route_table_association" "private" {
  for_each       = local.subnets
  subnet_id      = aws_subnet.private[each.key].id
  route_table_id = aws_route_table.private[each.value.env].id
}

resource "aws_vpc_peering_connection" "platform_source" {
  vpc_id      = aws_vpc.environment["platform"].id
  peer_vpc_id = aws_vpc.environment["source"].id
  auto_accept = true
  tags        = { Name = "${var.name}-platform-source" }
}

resource "aws_vpc_peering_connection_options" "dns" {
  vpc_peering_connection_id = aws_vpc_peering_connection.platform_source.id
  requester { allow_remote_vpc_dns_resolution = true }
  accepter { allow_remote_vpc_dns_resolution = true }
}

resource "aws_route" "peer" {
  for_each                  = local.environments
  route_table_id            = aws_route_table.private[each.key].id
  destination_cidr_block    = local.environments[each.key == "source" ? "platform" : "source"]
  vpc_peering_connection_id = aws_vpc_peering_connection.platform_source.id
}

resource "aws_security_group" "role" {
  for_each    = { platform_control = "platform", source_api = "source", source_db = "source" }
  name        = "${var.name}-${each.key}"
  description = "T1 ${each.key}; only explicit peer rules, no public ingress or default egress"
  vpc_id      = aws_vpc.environment[each.value].id
  tags        = { Name = "${var.name}-${each.key}", environment = each.value, component = each.value }
}

resource "aws_vpc_security_group_ingress_rule" "source" {
  for_each                     = { source_api = 443, source_db = 5432 }
  security_group_id            = aws_security_group.role[each.key].id
  referenced_security_group_id = aws_security_group.role["platform_control"].id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  description                  = "Platform control to ${each.key}; app and IAM authorization remain required"
  tags                         = { Name = "${var.name}-to-${each.key}", component = "source" }
  depends_on                   = [aws_vpc_peering_connection.platform_source]
}

resource "aws_vpc_security_group_egress_rule" "platform" {
  for_each                     = { source_api = 443, source_db = 5432 }
  security_group_id            = aws_security_group.role["platform_control"].id
  referenced_security_group_id = aws_security_group.role[each.key].id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  description                  = "Platform control to ${each.key} over private peering"
  tags                         = { Name = "${var.name}-from-platform-to-${each.key}" }
  depends_on                   = [aws_vpc_peering_connection.platform_source]
}
