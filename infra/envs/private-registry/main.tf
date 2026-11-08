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

# T49 (2026-11-08 user decision): the platform builds candidates itself; GitHub is not in the happy path.
# The platform host role pushes to this one repository only. Destinations pull with a token of a
# pull-only role that the host assumes, so a destination credential can never push.
data "aws_iam_role" "platform_host" {
  name = var.platform_host_role
}

resource "aws_iam_role_policy" "platform_candidate_push" {
  name = "candidate-push-exact-repository"
  role = data.aws_iam_role.platform_host.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "RegistryAuthentication", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Sid = "PushExactRepository", Effect = "Allow",
      Action = concat(local.pull_actions, [
        "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"
    ]), Resource = aws_ecr_repository.application.arn },
    { Sid = "AssumeCandidatePuller", Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.candidate_puller.arn }
  ] })
}

resource "aws_iam_role" "candidate_puller" {
  name                 = "bravo-candidate-puller"
  description          = "Pull-only token for destinations that run platform-built candidates"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { AWS = data.aws_iam_role.platform_host.arn }
  }] })
}

resource "aws_iam_role_policy" "candidate_puller" {
  name = "pull-exact-repository"
  role = aws_iam_role.candidate_puller.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "RegistryAuthentication", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Sid = "PullExactRepository", Effect = "Allow", Action = local.pull_actions, Resource = aws_ecr_repository.application.arn }
  ] })
}

output "candidate_build_contract" {
  sensitive = true
  value = {
    repository_url     = aws_ecr_repository.application.repository_url
    puller_role_arn    = aws_iam_role.candidate_puller.arn
    platform_host_role = data.aws_iam_role.platform_host.name
  }
}
