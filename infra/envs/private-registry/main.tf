# The foundation owns this existing provider. This stack owns no node roles,
# network resources, registry-wide settings or workload credentials.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
  lifecycle {
    postcondition {
      condition     = contains(self.client_id_list, "sts.amazonaws.com")
      error_message = "The existing GitHub provider must allow the STS audience."
    }
  }
}

resource "aws_ecr_repository" "application" {
  name                 = "bravo/demo-chat"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  encryption_configuration {
    # ECR's AWS-managed KMS key; no shared workload key policy is changed.
    encryption_type = "KMS"
  }
  image_scanning_configuration { scan_on_push = true }
  lifecycle { prevent_destroy = true }
}

locals {
  # The repository was renamed demo-chat -> demo-app (11/8); the OIDC subject carries the current name and the
  # same immutable repository ID. The ECR repository name bravo/demo-chat stays.
  github_subjects = ["repo:Softbank-Hackathon-Finals@${var.github_owner_id}/demo-app@${var.github_repository_id}:ref:refs/heads/main"]
  pull_actions    = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
}

resource "aws_iam_role" "publisher" {
  name                 = "bravo-private-image-publisher"
  description          = "T7 demo-chat GHCR to private ECR digest-preserving publication"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = local.github_subjects
    } }
  }] })
}

resource "aws_iam_role_policy" "publisher" {
  name = "publish-exact-demo-repository"
  role = aws_iam_role.publisher.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "RegistryAuthentication", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Sid = "PublishAndVerifyExactRepository", Effect = "Allow",
      Action = concat(local.pull_actions, [
        "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
        "ecr:DescribeImages", "ecr:DescribeRepositories"
    ]), Resource = aws_ecr_repository.application.arn }
  ] })
}

output "registry_contract" {
  sensitive = true
  value = {
    account_id         = var.account_id
    region             = var.region
    repository_name    = aws_ecr_repository.application.name
    repository_arn     = aws_ecr_repository.application.arn
    repository_url     = aws_ecr_repository.application.repository_url
    publisher_role_arn = aws_iam_role.publisher.arn
    # A foundation owner may attach this exact repository read policy. This
    # output does not claim that an existing node/host role has been modified.
    pull_policy = { Version = "2012-10-17", Statement = [
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      { Effect = "Allow", Action = local.pull_actions, Resource = aws_ecr_repository.application.arn }
    ] }
  }
}
