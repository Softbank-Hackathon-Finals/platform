terraform {
  required_version = ">= 1.16.5, < 2.0.0"
  backend "s3" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.71.0" }
  }
}

provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = { project = "bravo-platform", component = "platform", "managed-by" = "terraform" }
  }
}

variable "account_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the reviewed development account."
  }
}

variable "platform_host_role" {
  type = string
  validation {
    condition     = var.platform_host_role == "bravo-platform-runtime"
    error_message = "Only the existing T7 platform host may receive this pull policy."
  }
}

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
  lifecycle {
    postcondition {
      condition     = contains(self.client_id_list, "sts.amazonaws.com")
      error_message = "The existing GitHub provider must permit the STS audience."
    }
  }
}

data "aws_iam_role" "platform_host" {
  name = var.platform_host_role
  lifecycle {
    postcondition {
      condition = self.arn == "arn:aws:iam::${var.account_id}:role/${var.platform_host_role}" && alltrue([
        for key, value in { project = "bravo-platform", component = "platform", "managed-by" = "terraform" } :
        lookup(self.tags, key, "") == value
      ])
      error_message = "The platform host role must match the reviewed account and ownership tags."
    }
  }
}

resource "aws_ecr_repository" "executor" {
  name                 = "bravo/cutover-executor"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  encryption_configuration { encryption_type = "KMS" }
  image_scanning_configuration { scan_on_push = true }
  lifecycle { prevent_destroy = true }
}

locals {
  subject = "repo:Softbank-Hackathon-Finals@350259663/platform@1443045223:ref:refs/heads/develop"
  pull    = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
  authentication = {
    Sid = "RegistryAuthentication", Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*"
  }
}

resource "aws_iam_role" "publisher" {
  name                 = "bravo-executor-image-publisher"
  description          = "Publish reviewed platform Executor images to their dedicated repository"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = local.subject
    } }
  }] })
}

resource "aws_iam_role_policy" "publisher" {
  name = "publish-exact-executor-repository"
  role = aws_iam_role.publisher.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [local.authentication, {
    Sid = "PublishAndVerifyExecutor", Effect = "Allow",
    Action = concat(local.pull, ["ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
    "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:DescribeImages"]),
    Resource = aws_ecr_repository.executor.arn
  }] })
}

resource "aws_iam_role_policy" "host_pull" {
  name = "pull-exact-executor-repository"
  role = data.aws_iam_role.platform_host.name
  policy = jsonencode({ Version = "2012-10-17", Statement = [local.authentication, {
    Sid      = "ReadExecutor", Effect = "Allow", Action = local.pull,
    Resource = aws_ecr_repository.executor.arn
  }] })
}

output "publication_contract" {
  sensitive = true
  value = {
    account_id         = var.account_id
    region             = "ap-northeast-2"
    repository_name    = aws_ecr_repository.executor.name
    repository_url     = aws_ecr_repository.executor.repository_url
    publisher_role_arn = aws_iam_role.publisher.arn
    platform_host_role = data.aws_iam_role.platform_host.name
    subject            = local.subject
  }
}
