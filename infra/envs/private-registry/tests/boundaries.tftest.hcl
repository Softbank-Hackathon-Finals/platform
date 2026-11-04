mock_provider "aws" {
  mock_data "aws_iam_openid_connect_provider" {
    defaults = {
      arn            = "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"
      client_id_list = ["sts.amazonaws.com"]
    }
  }
}
variables {
  account_id           = "000000000000"
  github_owner_id      = "10001"
  github_repository_id = "20002"
}
run "exact_publisher_boundary" {
  command = apply
  assert {
    condition     = aws_ecr_repository.application.name == "bravo/demo-chat" && aws_ecr_repository.application.image_tag_mutability == "IMMUTABLE" && !aws_ecr_repository.application.force_delete
    error_message = "The private repository must preserve immutable deployment evidence."
  }
  assert {
    condition     = aws_ecr_repository.application.encryption_configuration[0].encryption_type == "KMS" && aws_ecr_repository.application.image_scanning_configuration[0].scan_on_push
    error_message = "Encryption and on-push scanning must remain enabled."
  }
  assert {
    condition     = jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == ["repo:Softbank-Hackathon-Finals@10001/demo-chat@20002:ref:refs/heads/main"]
    error_message = "OIDC must bind the exact immutable repository identity and branch."
  }
  assert {
    condition     = jsondecode(aws_iam_role.publisher.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    error_message = "OIDC must require the STS audience."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.publisher.policy).Statement[1].Resource == aws_ecr_repository.application.arn && !contains(jsondecode(aws_iam_role_policy.publisher.policy).Statement[1].Action, "ecr:DeleteRepository") && !contains(jsondecode(aws_iam_role_policy.publisher.policy).Statement[1].Action, "ecr:SetRepositoryPolicy")
    error_message = "Publisher must only upload/read the exact repository, with no policy or deletion control."
  }
}
run "reject_wildcard_owner_identity" {
  command = plan
  variables { github_owner_id = "100*" }
  expect_failures = [var.github_owner_id]
}
run "reject_non_numeric_repository_identity" {
  command = plan
  variables { github_repository_id = "other/repository" }
  expect_failures = [var.github_repository_id]
}
