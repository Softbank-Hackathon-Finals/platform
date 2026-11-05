mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { arn = "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_BravoAdmin_0123456789abcdef/operator" }
  }
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_BravoAdmin_0123456789abcdef" }
  }
}
variables {
  account_id            = "111122223333"
  custodian_role_arns   = ["arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_BravoAdmin_0123456789abcdef"]
  writer_principal_arns = ["arn:aws:iam::111122223333:role/bravo-platform-runtime"]
  reader_principal_arns = ["arn:aws:iam::111122223333:role/bravo-console"]
}

run "bucket_is_locked_private_and_encrypted" {
  command = apply
  assert {
    condition     = aws_s3_bucket.audit.object_lock_enabled && !aws_s3_bucket.audit.force_destroy
    error_message = "Object Lock is enabled at creation and terraform destroy never empties the bucket."
  }
  assert {
    condition     = one(aws_s3_bucket_object_lock_configuration.audit.rule).default_retention[0].mode == "COMPLIANCE" && one(aws_s3_bucket_object_lock_configuration.audit.rule).default_retention[0].days == 1
    error_message = "Records default to COMPLIANCE retention; the demo keeps them one day."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.audit.block_public_acls && aws_s3_bucket_public_access_block.audit.block_public_policy && aws_s3_bucket_public_access_block.audit.ignore_public_acls && aws_s3_bucket_public_access_block.audit.restrict_public_buckets
    error_message = "All public access is blocked."
  }
  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.audit.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms" && one(aws_s3_bucket_server_side_encryption_configuration.audit.rule).apply_server_side_encryption_by_default[0].kms_master_key_id == aws_kms_key.audit.arn
    error_message = "Records are encrypted with this stack's key."
  }
}

run "policy_only_takes_rights_away" {
  command = apply
  assert {
    condition     = alltrue([for s in jsondecode(aws_s3_bucket_policy.audit.policy).Statement : s.Effect == "Deny" && s.Principal == "*"])
    error_message = "The bucket policy never grants access."
  }
  assert {
    condition = alltrue([for sid in ["DenyRecordDeletionExceptCustodians", "DenyProtectionChangesExceptCustodians"] :
      tolist(one([for s in jsondecode(aws_s3_bucket_policy.audit.policy).Statement : s.Condition.ArnNotEquals["aws:PrincipalArn"] if s.Sid == sid])) == var.custodian_role_arns
    ])
    error_message = "Deletion and protection changes are denied to everyone except the exact custodian roles."
  }
  assert {
    condition = alltrue([for action in ["s3:DeleteBucketPolicy", "s3:PutBucketPolicy", "s3:PutBucketObjectLockConfiguration", "s3:PutBucketVersioning", "s3:PutEncryptionConfiguration"] :
      contains(one([for s in jsondecode(aws_s3_bucket_policy.audit.policy).Statement : s.Action if s.Sid == "DenyProtectionChangesExceptCustodians"]), action)
    ])
    error_message = "Nobody else can lift the lock by changing the policy, lock configuration, versioning or encryption."
  }
  assert {
    condition     = one([for s in jsondecode(aws_s3_bucket_policy.audit.policy).Statement : s.Action if s.Sid == "DenyGovernanceBypass"]) == "s3:BypassGovernanceRetention" && !contains(keys(one([for s in jsondecode(aws_s3_bucket_policy.audit.policy).Statement : s if s.Sid == "DenyGovernanceBypass"])), "Condition")
    error_message = "Governance bypass is refused to everyone, custodians included."
  }
  assert {
    condition     = one([for s in jsondecode(aws_s3_bucket_policy.audit.policy).Statement : s.Condition.Bool["aws:SecureTransport"] if s.Sid == "DenyInsecureTransport"]) == "false"
    error_message = "Plain HTTP is refused."
  }
}

run "writer_cannot_delete_list_or_reconfigure" {
  command = apply
  assert {
    condition = length(setintersection(
      toset(flatten([for s in jsondecode(aws_iam_role_policy.writer.policy).Statement : s.Action])),
      toset(["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:ListBucketVersions", "s3:PutBucketPolicy", "s3:PutBucketObjectLockConfiguration", "s3:BypassGovernanceRetention"])
    )) == 0
    error_message = "The writer only puts, reads back and sets retention on new records."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.writer.policy).Statement : s.Condition.StringLike["s3:prefix"] if s.Action == ["s3:ListBucket"]]) == ["audit/*"]
    error_message = "The writer lists only its own prefix, to resume a run's chain."
  }
  assert {
    condition     = tolist(jsondecode(aws_iam_role.audit["writer"].assume_role_policy).Statement[0].Condition.ArnEquals["aws:PrincipalArn"]) == var.writer_principal_arns
    error_message = "Only the listed principals may assume the writer role."
  }
  assert {
    condition = length(setintersection(
      toset(flatten([for s in jsondecode(aws_iam_role_policy.reader.policy).Statement : s.Action])),
      toset(["s3:PutObject", "s3:PutObjectRetention", "s3:DeleteObject", "s3:DeleteObjectVersion"])
    )) == 0
    error_message = "The search role is read-only."
  }
}

run "applier_must_be_a_custodian" {
  command = plan
  variables {
    custodian_role_arns = ["arn:aws:iam::111122223333:role/someone-else"]
  }
  expect_failures = [aws_s3_bucket_policy.audit]
}
