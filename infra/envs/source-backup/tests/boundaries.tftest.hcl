mock_provider "aws" {
  mock_data "aws_eks_cluster" {
    defaults = {
      identity = [{ oidc = [{ issuer = "https://oidc.eks.ap-northeast-2.amazonaws.com/id/EXAMPLE" }] }]
    }
  }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.ap-northeast-2.amazonaws.com/id/EXAMPLE" }
  }
  mock_data "aws_caller_identity" {
    defaults = { arn = "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_BravoAdmin_0123456789abcdef/operator" }
  }
  mock_data "aws_iam_session_context" {
    defaults = { issuer_arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_BravoAdmin_0123456789abcdef" }
  }
}
variables {
  account_id          = "111122223333"
  custodian_role_arns = ["arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_BravoAdmin_0123456789abcdef"]
}

run "only_custodians_delete_or_unprotect" {
  command = apply
  assert {
    condition     = alltrue([for s in jsondecode(aws_s3_bucket_policy.backup.policy).Statement : s.Effect == "Deny" && s.Principal == "*"])
    error_message = "The bucket policy only takes rights away; it never grants access."
  }
  assert {
    condition = alltrue([for sid in ["DenyBackupDeletionExceptCustodians", "DenyProtectionChangesExceptCustodians"] :
      tolist(one([for s in jsondecode(aws_s3_bucket_policy.backup.policy).Statement : s.Condition.ArnNotEquals["aws:PrincipalArn"] if s.Sid == sid])) == var.custodian_role_arns
    ])
    error_message = "Deletion and protection changes are denied to everyone except the exact custodian roles."
  }
  assert {
    condition = alltrue([for action in ["s3:DeleteObject", "s3:DeleteObjectVersion"] :
      contains(one([for s in jsondecode(aws_s3_bucket_policy.backup.policy).Statement : s.Action if s.Sid == "DenyBackupDeletionExceptCustodians"]), action)
    ])
    error_message = "Both current objects and previous versions are protected."
  }
  assert {
    condition = alltrue([for action in ["s3:DeleteBucket", "s3:DeleteBucketPolicy", "s3:PutBucketPolicy", "s3:PutBucketVersioning", "s3:PutLifecycleConfiguration", "s3:PutEncryptionConfiguration"] :
      contains(one([for s in jsondecode(aws_s3_bucket_policy.backup.policy).Statement : s.Action if s.Sid == "DenyProtectionChangesExceptCustodians"]), action)
    ])
    error_message = "Nobody else can lift the protection by changing the policy, versioning, lifecycle or encryption."
  }
  assert {
    condition     = one([for s in jsondecode(aws_s3_bucket_policy.backup.policy).Statement : s.Condition.Bool["aws:SecureTransport"] if s.Sid == "DenyInsecureTransport"]) == "false"
    error_message = "Plain HTTP is refused."
  }
}

run "bucket_is_private_versioned_and_encrypted" {
  command = apply
  assert {
    condition     = aws_s3_bucket_public_access_block.backup.block_public_acls && aws_s3_bucket_public_access_block.backup.block_public_policy && aws_s3_bucket_public_access_block.backup.ignore_public_acls && aws_s3_bucket_public_access_block.backup.restrict_public_buckets
    error_message = "All public access is blocked."
  }
  assert {
    condition     = aws_s3_bucket_versioning.backup.versioning_configuration[0].status == "Enabled"
    error_message = "A deletion by a custodian still leaves a previous version."
  }
  assert {
    condition     = one(aws_s3_bucket_server_side_encryption_configuration.backup.rule).apply_server_side_encryption_by_default[0].sse_algorithm == "aws:kms" && one(aws_s3_bucket_server_side_encryption_configuration.backup.rule).apply_server_side_encryption_by_default[0].kms_master_key_id == aws_kms_key.backup.arn
    error_message = "Backups are encrypted with this stack's key, not the application key."
  }
  assert {
    condition     = !aws_s3_bucket.backup.force_destroy
    error_message = "terraform destroy must not empty the bucket; a custodian does that deliberately."
  }
  assert {
    condition     = one(aws_s3_bucket_lifecycle_configuration.backup.rule).noncurrent_version_expiration[0].noncurrent_days == 7 && length(one(aws_s3_bucket_lifecycle_configuration.backup.rule).expiration) == 0
    error_message = "Lifecycle only expires previous versions; current backups are never expired automatically."
  }
}

run "roles_are_bound_to_one_service_account_each" {
  command = apply
  assert {
    condition = alltrue([for key, sa in { writer = "system:serviceaccount:source-dev:source-db", reader = "system:serviceaccount:t35-restore-check:source-db-restore" } :
      jsondecode(aws_iam_role.barman[key].assume_role_policy).Statement[0].Condition.StringEquals["oidc.eks.ap-northeast-2.amazonaws.com/id/EXAMPLE:sub"] == sa
    ])
    error_message = "Each role trusts exactly one ServiceAccount of the source cluster."
  }
  assert {
    condition = alltrue(flatten([for policy in [aws_iam_role_policy.writer.policy, aws_iam_role_policy.reader.policy] : [
      for s in jsondecode(policy).Statement : s.Effect == "Allow" && alltrue([for a in s.Action : !strcontains(a, "*") && !startswith(a, "s3:Delete") && !startswith(a, "s3:PutBucket")])
    ]]))
    error_message = "Neither role can delete, use wildcards or change the bucket."
  }
  assert {
    condition     = sort(flatten([for s in jsondecode(aws_iam_role_policy.reader.policy).Statement : s.Action])) == tolist(["kms:Decrypt", "s3:GetObject", "s3:ListBucket"])
    error_message = "The restore role only reads."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.writer.policy).Statement : s.Resource if contains(s.Action, "s3:PutObject")]) == "arn:aws:s3:::bravo-source-backup-111122223333/t35/source-db/*"
    error_message = "The writer only writes below the registered prefix."
  }
}

run "applier_must_be_a_custodian" {
  command = plan
  override_data {
    target = data.aws_iam_session_context.applier
    values = { issuer_arn = "arn:aws:iam::111122223333:role/someone-else" }
  }
  expect_failures = [aws_s3_bucket_policy.backup]
}

run "custodians_are_exact_roles" {
  command = plan
  variables { custodian_role_arns = ["arn:aws:iam::111122223333:role/*"] }
  expect_failures = [var.custodian_role_arns]
}

run "restore_runs_outside_the_source_namespace" {
  command = plan
  variables { reader_service_account = { namespace = "source-dev", name = "source-db-restore" } }
  expect_failures = [var.reader_service_account]
}

run "source_declares_only_reviewed_resources" {
  command = plan
  assert {
    condition = sort(distinct(flatten([for name in fileset(path.module, "*.tf") : [
      for match in regexall("(?m)^resource \"([a-z0-9_]+)\"", file("${path.module}/${name}")) : match[0]
      ]]))) == sort([
      "aws_iam_role",
      "aws_iam_role_policy",
      "aws_kms_alias",
      "aws_kms_key",
      "aws_s3_bucket",
      "aws_s3_bucket_lifecycle_configuration",
      "aws_s3_bucket_ownership_controls",
      "aws_s3_bucket_policy",
      "aws_s3_bucket_public_access_block",
      "aws_s3_bucket_server_side_encryption_configuration",
      "aws_s3_bucket_versioning",
    ])
    error_message = "This stack declares exactly the reviewed resources. Review the deletion boundary before adding one."
  }
  assert {
    condition = alltrue([for name in fileset(path.module, "*.tf") :
      !strcontains(file("${path.module}/${name}"), "prevent_destroy")
    ])
    error_message = "The stack stays removable after the demo; force_destroy=false keeps the backups until a custodian empties the bucket."
  }
}
