mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
  mock_data "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_BravoDevelopmentAdministrator_0123456789abcdef" }
  }
}

variables {
  account_id       = "111122223333"
  region           = "ap-northeast-2"
  bucket_arn       = "arn:aws:s3:::example-attachments-111122223333"
  kms_key_arn      = "arn:aws:kms:ap-northeast-2:111122223333:key/0d9d7a52-7c1f-4f0a-9f0e-3a5b6c7d8e9f"
  trusted_role_arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_BravoDevelopmentAdministrator_0123456789abcdef"
}

run "one_sso_role_assumes_a_read_only_role" {
  command = plan
  assert {
    condition = jsondecode(aws_iam_role.reader.assume_role_policy) == {
      Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { AWS = var.trusted_role_arn }, Action = "sts:AssumeRole" }]
    }
    error_message = "Only the reviewed SSO role may assume the reader, with no other principal or action."
  }
  assert {
    condition     = aws_iam_role.reader.max_session_duration == 3600
    error_message = "Sessions last at most one hour."
  }
  assert {
    # The fixture is attachment_job.source_session_policy for these inputs; the Python suite checks that side.
    condition     = jsondecode(aws_iam_role_policy.reader.policy) == jsondecode(file("${path.module}/tests/session-policy.json"))
    error_message = "The role policy must equal the Job's session policy: list, read, S3-mediated decrypt, everything else denied."
  }
  assert {
    condition     = aws_iam_role_policy.reader.role == aws_iam_role.reader.name
    error_message = "The read policy belongs to the reader role."
  }
}

run "wrong_caller_account_rejected" {
  command = plan
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "222222222222" }
  }
  expect_failures = [aws_iam_role.reader]
}

run "role_with_different_path_rejected" {
  command = plan
  override_data {
    target = data.aws_iam_role.trusted
    values = { arn = "arn:aws:iam::111122223333:role/AWSReservedSSO_BravoDevelopmentAdministrator_0123456789abcdef" }
  }
  expect_failures = [aws_iam_role.reader]
}

run "other_permission_set_rejected" {
  command = plan
  variables { trusted_role_arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_ReadOnly_0123456789abcdef" }
  expect_failures = [var.trusted_role_arn]
}

run "foreign_account_role_rejected" {
  command = plan
  variables { trusted_role_arn = "arn:aws:iam::222222222222:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_BravoDevelopmentAdministrator_0123456789abcdef" }
  expect_failures = [var.trusted_role_arn]
}

run "ordinary_role_rejected" {
  command = plan
  variables { trusted_role_arn = "arn:aws:iam::111122223333:role/operator" }
  expect_failures = [var.trusted_role_arn]
}

run "iam_user_rejected" {
  command = plan
  variables { trusted_role_arn = "arn:aws:iam::111122223333:user/operator" }
  expect_failures = [var.trusted_role_arn]
}

run "session_arn_rejected" {
  command = plan
  variables { trusted_role_arn = "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_BravoDevelopmentAdministrator_0123456789abcdef/operator" }
  expect_failures = [var.trusted_role_arn]
}

run "wildcard_role_rejected" {
  command = plan
  variables { trusted_role_arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_BravoDevelopmentAdministrator_*" }
  expect_failures = [var.trusted_role_arn]
}

run "us_east_1_identity_center_path_supported" {
  command = plan
  variables { trusted_role_arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_BravoDevelopmentAdministrator_0123456789abcdef" }
  override_data {
    target = data.aws_iam_role.trusted
    values = { arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_BravoDevelopmentAdministrator_0123456789abcdef" }
  }
}

run "wildcard_bucket_rejected" {
  command = plan
  variables { bucket_arn = "arn:aws:s3:::*-111122223333" }
  expect_failures = [var.bucket_arn]
}

run "object_path_bucket_rejected" {
  command = plan
  variables { bucket_arn = "arn:aws:s3:::example-attachments-111122223333/*" }
  expect_failures = [var.bucket_arn]
}

run "key_alias_rejected" {
  command = plan
  variables { kms_key_arn = "arn:aws:kms:ap-northeast-2:111122223333:alias/example-application" }
  expect_failures = [var.kms_key_arn]
}

run "key_in_another_region_rejected" {
  command = plan
  variables { kms_key_arn = "arn:aws:kms:ap-northeast-1:111122223333:key/0d9d7a52-7c1f-4f0a-9f0e-3a5b6c7d8e9f" }
  expect_failures = [var.kms_key_arn]
}

run "key_in_another_account_rejected" {
  command = plan
  variables { kms_key_arn = "arn:aws:kms:ap-northeast-2:222222222222:key/0d9d7a52-7c1f-4f0a-9f0e-3a5b6c7d8e9f" }
  expect_failures = [var.kms_key_arn]
}

run "empty_region_rejected" {
  command = plan
  variables { region = "" }
  expect_failures = [var.region]
}
