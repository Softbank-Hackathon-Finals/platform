# Preserve the owner pilot state created before the module extraction.

moved {
  from = aws_vpc.environment
  to   = module.network.aws_vpc.environment
}

moved {
  from = aws_default_security_group.closed
  to   = module.network.aws_default_security_group.closed
}

moved {
  from = aws_subnet.private
  to   = module.network.aws_subnet.private
}

moved {
  from = aws_route_table.private
  to   = module.network.aws_route_table.private
}

moved {
  from = aws_route_table_association.private
  to   = module.network.aws_route_table_association.private
}

moved {
  from = aws_vpc_peering_connection.platform_source
  to   = module.network.aws_vpc_peering_connection.platform_source
}

moved {
  from = aws_vpc_peering_connection_options.dns
  to   = module.network.aws_vpc_peering_connection_options.dns
}

moved {
  from = aws_route.peer
  to   = module.network.aws_route.peer
}

moved {
  from = aws_security_group.role
  to   = module.network.aws_security_group.role
}

moved {
  from = aws_vpc_security_group_ingress_rule.source
  to   = module.network.aws_vpc_security_group_ingress_rule.source
}

moved {
  from = aws_vpc_security_group_egress_rule.platform
  to   = module.network.aws_vpc_security_group_egress_rule.platform
}

moved {
  from = aws_kms_key.flow_logs
  to   = module.network.aws_kms_key.flow_logs
}

moved {
  from = aws_cloudwatch_log_group.vpc
  to   = module.network.aws_cloudwatch_log_group.vpc
}

moved {
  from = aws_iam_role.flow_logs
  to   = module.network.aws_iam_role.flow_logs
}

moved {
  from = aws_iam_role_policy.flow_logs
  to   = module.network.aws_iam_role_policy.flow_logs
}

moved {
  from = aws_flow_log.vpc
  to   = module.network.aws_flow_log.vpc
}
