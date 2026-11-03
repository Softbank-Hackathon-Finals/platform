output "foundation" {
  description = "T6/T7 integration inputs. These are resource identifiers, not credentials."
  value = {
    account_id      = var.account_id
    region          = var.region
    name            = var.name
    vpcs            = { for name, vpc in aws_vpc.environment : name => { id = vpc.id, cidr = vpc.cidr_block } }
    subnets         = { for name, subnet in aws_subnet.private : name => { id = subnet.id, az = subnet.availability_zone } }
    route_tables    = { for name, rt in aws_route_table.private : name => rt.id }
    security_groups = { for name, sg in aws_security_group.role : name => sg.id }
    peering_id      = aws_vpc_peering_connection.platform_source.id
    flow_log_ids    = [for log in aws_flow_log.vpc : log.id]
    log_groups      = [for log in aws_cloudwatch_log_group.vpc : log.name]
    log_kms_key_id  = aws_kms_key.flow_logs.id
  }
}
