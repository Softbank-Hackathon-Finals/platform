mock_provider "aws" {
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::111122223333:oidc-provider/bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/bravo-findings-pull", id = "bravo-findings-pull" }
  }
}

variables {
  account_id = "111122223333"
  issuer_url = "https://bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform"
}

run "only_the_puller_service_account_assumes_the_role" {
  command = apply
  assert {
    condition = jsondecode(aws_iam_role.pull.assume_role_policy).Statement[0].Condition.StringEquals == {
      "bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform:sub" = "system:serviceaccount:bravo-security:bravo-findings-pull"
      "bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform:aud" = "sts.amazonaws.com"
    }
    error_message = "Exactly one ServiceAccount subject and the STS audience."
  }
  assert {
    condition     = jsondecode(aws_iam_role.pull.assume_role_policy).Statement[0].Action == "sts:AssumeRoleWithWebIdentity"
    error_message = "Web identity only."
  }
}

run "guardduty_read_only_in_one_region" {
  command = apply
  assert {
    condition = toset(jsondecode(aws_iam_role_policy.guardduty_read.policy).Statement[0].Action) == toset([
      "guardduty:ListDetectors", "guardduty:GetDetector", "guardduty:ListFindings", "guardduty:GetFindings"
    ])
    error_message = "Read actions only: no detector, sample, archive or update calls."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.guardduty_read.policy).Statement[0].Condition.StringEquals["aws:RequestedRegion"] == "ap-northeast-2"
    error_message = "One region."
  }
}

run "wildcard_subject_is_refused" {
  command = plan
  variables {
    service_account = "system:serviceaccount:bravo-security:*"
  }
  expect_failures = [var.service_account]
}
