data "aws_partition" "current" {}

resource "aws_iam_role_policy" "private_ecr_pull" {
  count = var.private_ecr_repository_name == null ? 0 : 1
  name  = "private-ecr-pull"
  role  = aws_iam_role.node.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RegistryAuthentication"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*" # This API does not support repository-level resources.
      },
      {
        Sid      = "ReadSelectedRepository"
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = "arn:${data.aws_partition.current.partition}:ecr:${var.region}:${var.account_id}:repository/${var.private_ecr_repository_name}"
      }
    ]
  })
}
