data "aws_partition" "current" {}

locals {
  log_arns = [for env in keys(local.environments) : "arn:${data.aws_partition.current.partition}:logs:${var.region}:${var.account_id}:log-group:/bravo/${var.name}/${env}/vpc"]
}

resource "aws_kms_key" "flow_logs" {
  description             = "${var.name} VPC flow logs"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${var.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "OnlyTheseLogGroups"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.region}.amazonaws.com" }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource  = "*"
        Condition = { ArnEquals = { "kms:EncryptionContext:aws:logs:arn" = local.log_arns } }
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "vpc" {
  for_each          = local.environments
  name              = "/bravo/${var.name}/${each.key}/vpc"
  retention_in_days = 7
  kms_key_id        = aws_kms_key.flow_logs.arn
  tags              = { environment = each.key, component = each.key }
}

resource "aws_iam_role" "flow_logs" {
  name = "${var.name}-vpc-flow-logs"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:${data.aws_partition.current.partition}:ec2:${var.region}:${var.account_id}:vpc-flow-log/*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "flow_logs" {
  name = "write-only-project-vpc-logs"
  role = aws_iam_role.flow_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = [for arn in local.log_arns : "${arn}:*"]
      },
      {
        # CloudWatch DescribeLogGroups does not support resource-level permissions.
        Effect   = "Allow"
        Action   = "logs:DescribeLogGroups"
        Resource = "*"
      }
    ]
  })
}

resource "aws_flow_log" "vpc" {
  for_each                 = local.environments
  vpc_id                   = aws_vpc.environment[each.key].id
  traffic_type             = "ALL"
  log_destination          = aws_cloudwatch_log_group.vpc[each.key].arn
  iam_role_arn             = aws_iam_role.flow_logs.arn
  max_aggregation_interval = 60
  tags                     = { Name = "${var.name}-${each.key}-flow", environment = each.key, component = each.key }
  depends_on               = [aws_iam_role_policy.flow_logs]
}
