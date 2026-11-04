# T35 source CNPG backups. source-eks owns EKS and its OIDC provider, source-workloads owns the
# database and the app. This state only reads them and owns the backup bucket, its key and two Barman roles.
data "aws_eks_cluster" "source" { name = var.cluster_name }
data "aws_iam_openid_connect_provider" "source" {
  url = data.aws_eks_cluster.source.identity[0].oidc[0].issuer
}
data "aws_caller_identity" "applier" {}
data "aws_iam_session_context" "applier" { arn = data.aws_caller_identity.applier.arn }

locals {
  issuer     = replace(data.aws_eks_cluster.source.identity[0].oidc[0].issuer, "https://", "")
  bucket     = "${var.name}-${var.account_id}"
  bucket_arn = "arn:aws:s3:::${local.bucket}"
  objects    = "${local.bucket_arn}/${var.prefix}*"
  subjects = {
    writer = "system:serviceaccount:${var.writer_service_account.namespace}:${var.writer_service_account.name}"
    reader = "system:serviceaccount:${var.reader_service_account.namespace}:${var.reader_service_account.name}"
  }
  kms_via_s3 = { StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" } }
  # Everyone but the custodians, including the account's other roles, the app and the Executor.
  not_custodian = { ArnNotEquals = { "aws:PrincipalArn" = var.custodian_role_arns } }
}

resource "aws_kms_key" "backup" {
  description             = "T35 source CNPG backups"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "EnableAccountIAM", Effect = "Allow",
    Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }, Action = "kms:*", Resource = "*" }
  ] })
}
resource "aws_kms_alias" "backup" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.backup.key_id
}

resource "aws_s3_bucket" "backup" {
  bucket        = local.bucket
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "backup" {
  bucket                  = aws_s3_bucket.backup.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_versioning" "backup" {
  bucket = aws_s3_bucket.backup.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.backup.arn
    }
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule {
    id     = "previous-versions-and-abandoned-uploads"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = var.noncurrent_version_days }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
  depends_on = [aws_s3_bucket_versioning.backup]
}
resource "aws_s3_bucket_policy" "backup" {
  bucket = aws_s3_bucket.backup.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid      = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
      Resource = [local.bucket_arn, "${local.bucket_arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    { Sid    = "DenyBackupDeletionExceptCustodians", Effect = "Deny", Principal = "*",
      Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "${local.bucket_arn}/*",
    Condition = local.not_custodian },
    { Sid = "DenyProtectionChangesExceptCustodians", Effect = "Deny", Principal = "*",
      Action = ["s3:DeleteBucket", "s3:DeleteBucketPolicy", "s3:PutBucketPolicy", "s3:PutBucketVersioning",
        "s3:PutLifecycleConfiguration", "s3:PutEncryptionConfiguration", "s3:PutBucketPublicAccessBlock",
      "s3:PutBucketOwnershipControls"],
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
  depends_on = [aws_s3_bucket_public_access_block.backup]
}

resource "aws_iam_role" "barman" {
  for_each = local.subjects
  name     = "${var.name}-${each.key}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.source.arn },
    Condition = { StringEquals = {
      "${local.issuer}:sub" = each.value, "${local.issuer}:aud" = "sts.amazonaws.com"
    } }
  }] })
}
# Barman needs Put/Get/List and multipart abort to archive WAL and base backups. Deletion is only for
# its retention policy, which T35 does not enable.
resource "aws_iam_role_policy" "writer" {
  name = "t35-backup-write"
  role = aws_iam_role.barman["writer"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = local.bucket_arn },
    { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"], Resource = local.objects },
    { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = aws_kms_key.backup.arn, Condition = local.kms_via_s3 }
  ] })
}
resource "aws_iam_role_policy" "reader" {
  name = "t35-backup-read"
  role = aws_iam_role.barman["reader"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = local.bucket_arn },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = local.objects },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.backup.arn, Condition = local.kms_via_s3 }
  ] })
}

output "backup_registration" {
  sensitive = true
  value = {
    region          = var.region
    bucket          = aws_s3_bucket.backup.id
    destination     = "s3://${aws_s3_bucket.backup.id}/${var.prefix}"
    kms_key_arn     = aws_kms_key.backup.arn
    writer_role_arn = aws_iam_role.barman["writer"].arn
    reader_role_arn = aws_iam_role.barman["reader"].arn
    writer          = var.writer_service_account
    reader          = var.reader_service_account
    custodians      = var.custodian_role_arns
  }
}
