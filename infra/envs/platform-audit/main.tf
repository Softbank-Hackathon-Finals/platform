# T24 platform audit ledger. One Object Lock bucket in the platform account, its own key, a writer
# role for the T10 side and a read-only search role. Records are write-once (If-None-Match) and kept
# in COMPLIANCE mode; this state never owns the records themselves.
data "aws_caller_identity" "applier" {}
data "aws_iam_session_context" "applier" { arn = data.aws_caller_identity.applier.arn }

locals {
  bucket     = "${var.name}-${var.account_id}"
  bucket_arn = "arn:aws:s3:::${local.bucket}"
  objects    = "${local.bucket_arn}/${var.prefix}*"
  kms_via_s3 = { StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" } }
  # Everyone but the custodians, including the writer and reader roles below.
  not_custodian = { ArnNotEquals = { "aws:PrincipalArn" = var.custodian_role_arns } }
  roles = {
    writer = var.writer_principal_arns
    reader = var.reader_principal_arns
  }
}

resource "aws_kms_key" "audit" {
  description             = "T24 platform audit ledger"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "EnableAccountIAM", Effect = "Allow",
    Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }, Action = "kms:*", Resource = "*" }
  ] })
}
resource "aws_kms_alias" "audit" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.audit.key_id
}

resource "aws_s3_bucket" "audit" {
  bucket              = local.bucket
  object_lock_enabled = true
  force_destroy       = false
}
resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.audit]
}
resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.audit.arn
    }
  }
}
resource "aws_s3_bucket_policy" "audit" {
  bucket = aws_s3_bucket.audit.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid      = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
      Resource = [local.bucket_arn, "${local.bucket_arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    # COMPLIANCE already refuses version deletion during retention. This also refuses delete markers
    # and post-retention clean-up to everyone but a custodian.
    { Sid    = "DenyRecordDeletionExceptCustodians", Effect = "Deny", Principal = "*",
      Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "${local.bucket_arn}/*",
    Condition = local.not_custodian },
    { Sid = "DenyGovernanceBypass", Effect = "Deny", Principal = "*",
    Action = "s3:BypassGovernanceRetention", Resource = "${local.bucket_arn}/*" },
    # Review #257 P1: Object Lock keeps an old version but does not stop a new one. Every write
    # must carry If-None-Match, so a record key can never gain a second version.
    { Sid    = "DenyUnconditionalWrites", Effect = "Deny", Principal = "*",
      Action = "s3:PutObject", Resource = "${local.bucket_arn}/*",
    Condition = { Null = { "s3:if-none-match" = "true" } } },
    { Sid = "DenyProtectionChangesExceptCustodians", Effect = "Deny", Principal = "*",
      Action = ["s3:DeleteBucket", "s3:DeleteBucketPolicy", "s3:PutBucketPolicy", "s3:PutBucketVersioning",
        "s3:PutBucketObjectLockConfiguration", "s3:PutLifecycleConfiguration", "s3:PutEncryptionConfiguration",
      "s3:PutBucketPublicAccessBlock", "s3:PutBucketOwnershipControls"],
    Resource = local.bucket_arn, Condition = local.not_custodian }
  ] })
  # After this policy, only a custodian can change it. The applier must be one, or nobody but the
  # account root could correct a mistake.
  lifecycle {
    precondition {
      condition     = contains(var.custodian_role_arns, data.aws_iam_session_context.applier.issuer_arn)
      error_message = "Apply as one of custodian_role_arns."
    }
  }
  depends_on = [aws_s3_bucket_public_access_block.audit]
}

resource "aws_iam_role" "audit" {
  for_each             = local.roles
  name                 = "${var.name}-${each.key}"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRole",
    Principal = { AWS = "arn:aws:iam::${var.account_id}:root" },
    Condition = { ArnEquals = { "aws:PrincipalArn" = each.value } }
  }] })
}
# The sink writes with If-None-Match and an explicit COMPLIANCE retain-until date, then reads the
# version back to confirm the retention. Listing its own prefix (all versions) lets a writer resume
# a run's chain from what is actually stored and notice a rewritten record. It never deletes or
# changes the bucket.
resource "aws_iam_role_policy" "writer" {
  name = "t24-audit-write"
  role = aws_iam_role.audit["writer"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:ListBucketVersions"], Resource = local.bucket_arn,
    Condition = { StringLike = { "s3:prefix" = ["${var.prefix}*"] } } },
    { Effect = "Allow", Action = ["s3:PutObject", "s3:PutObjectRetention", "s3:GetObject", "s3:GetObjectVersion",
    "s3:GetObjectRetention"], Resource = local.objects },
    { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.audit.arn, Condition = local.kms_via_s3 }
  ] })
}
resource "aws_iam_role_policy" "reader" {
  name = "t24-audit-read"
  role = aws_iam_role.audit["reader"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:ListBucketVersions"], Resource = local.bucket_arn,
    Condition = { StringLike = { "s3:prefix" = ["${var.prefix}*"] } } },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectRetention"], Resource = local.objects },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.audit.arn, Condition = local.kms_via_s3 }
  ] })
}

output "audit_registration" {
  sensitive = true
  value = {
    region          = var.region
    bucket          = aws_s3_bucket.audit.id
    sink            = "s3://${aws_s3_bucket.audit.id}/${trimsuffix(var.prefix, "/")}"
    kms_key_arn     = aws_kms_key.audit.arn
    retention_days  = var.retention_days
    writer_role_arn = aws_iam_role.audit["writer"].arn
    reader_role_arn = aws_iam_role.audit["reader"].arn
    custodians      = var.custodian_role_arns
  }
}
