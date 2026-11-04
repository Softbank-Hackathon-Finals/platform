mock_provider "aws" {
  mock_data "aws_iam_openid_connect_provider" {
    defaults = {
      arn            = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"
      client_id_list = ["sts.amazonaws.com"]
    }
  }
  mock_data "aws_iam_role" {
    defaults = {
      name = "bravo-platform-runtime"
      arn  = "arn:aws:iam::111122223333:role/bravo-platform-runtime"
      tags = { project = "bravo-platform", component = "platform", "managed-by" = "terraform" }
    }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:ap-northeast-2:111122223333:repository/bravo/cutover-executor" }
  }
}

variables {
  account_id         = "111122223333"
  platform_host_role = "bravo-platform-runtime"
}

run "exact_publication_and_read_boundaries" {
  command = apply
  assert {
    condition = (aws_ecr_repository.executor.name == "bravo/cutover-executor" &&
      aws_ecr_repository.executor.image_tag_mutability == "IMMUTABLE" &&
      !aws_ecr_repository.executor.force_delete &&
      aws_ecr_repository.executor.encryption_configuration[0].encryption_type == "KMS" &&
    aws_ecr_repository.executor.image_scanning_configuration[0].scan_on_push)
    error_message = "The independent executor repository must preserve immutable scanned encrypted artifacts."
  }
  assert {
    condition = jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Condition.StringEquals == {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:Softbank-Hackathon-Finals@350259663/platform@1443045223:ref:refs/heads/develop"
    }
    error_message = "Trust must bind the immutable platform identity and reviewed develop branch."
  }
  assert {
    condition = (jsondecode(aws_iam_role_policy.publisher.policy).Statement[1].Resource == aws_ecr_repository.executor.arn &&
      jsondecode(aws_iam_role_policy.host_pull.policy).Statement[1].Resource == aws_ecr_repository.executor.arn &&
      toset(jsondecode(aws_iam_role_policy.host_pull.policy).Statement[1].Action) == toset([
        "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"
    ]))
    error_message = "The publisher and existing host may not gain other repository access or host write access."
  }
}

run "foreign_host_role_rejected" {
  command = plan
  variables { platform_host_role = "other-runtime" }
  expect_failures = [var.platform_host_role]
}

run "foreign_host_owner_rejected" {
  command = plan
  override_data {
    target = data.aws_iam_role.platform_host
    values = { tags = { project = "another-project", component = "platform", "managed-by" = "terraform" } }
  }
  expect_failures = [data.aws_iam_role.platform_host]
}

run "foreign_account_rejected" {
  command = plan
  override_data {
    target = data.aws_iam_role.platform_host
    values = { arn = "arn:aws:iam::222222222222:role/bravo-platform-runtime" }
  }
  expect_failures = [data.aws_iam_role.platform_host]
}

run "wrong_audience_rejected" {
  command = plan
  override_data {
    target = data.aws_iam_openid_connect_provider.github
    values = { client_id_list = ["other-audience"] }
  }
  expect_failures = [data.aws_iam_openid_connect_provider.github]
}
