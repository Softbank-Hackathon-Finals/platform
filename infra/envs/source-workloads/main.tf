# The foundation owns EKS, its existing OIDC provider, nodes, VPC and vpc-cni.
# This state only owns the additional workload resources declared below.
data "aws_eks_cluster" "source" { name = var.cluster_name }
data "aws_iam_openid_connect_provider" "source" {
  url = data.aws_eks_cluster.source.identity[0].oidc[0].issuer
}
locals {
  issuer    = replace(data.aws_eks_cluster.source.identity[0].oidc[0].issuer, "https://", "")
  trail_arn = "arn:aws:cloudtrail:${var.region}:${var.account_id}:trail/${var.name}"
  subjects = {
    ebs = "system:serviceaccount:kube-system:ebs-csi-controller-sa"
    app = "system:serviceaccount:source-dev:demo-chat"
    eso = "system:serviceaccount:source-dev:source-secret-reader"
  }
  # T14 demo-gate:1: the team SSO role takes the app role (and the t14-other role) for one
  # operations/controls/run.py session. AWS caps that chained session at one hour; the tool asks 15 minutes.
  t14_assume = { Effect = "Allow", Action = "sts:AssumeRole", Principal = { AWS = var.t14_operator_role_arn },
  Condition = { StringLike = { "sts:RoleSessionName" = "bravo-t14-*" } } }
}
data "aws_iam_role" "t14_operator" { name = reverse(split("/", var.t14_operator_role_arn))[0] }
resource "aws_iam_role" "workload" {
  for_each = local.subjects
  name     = "${var.name}-${each.key}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = concat([{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = data.aws_iam_openid_connect_provider.source.arn },
    Condition = { StringEquals = {
      "${local.issuer}:sub" = each.value, "${local.issuer}:aud" = "sts.amazonaws.com"
    } }
  }], each.key == "app" ? [local.t14_assume] : []) })
  lifecycle {
    precondition {
      condition     = data.aws_iam_role.t14_operator.arn == var.t14_operator_role_arn
      error_message = "IAM must return the exact reviewed SSO role ARN, including its reserved path and suffix."
    }
  }
}
# SEC-01.1 "other role": same account, no policy, so it cannot read the T14 Secret.
resource "aws_iam_role" "t14_other" {
  name               = "${var.name}-t14-other"
  description        = "T14 SEC-01.1 comparison role; holds no permissions"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [local.t14_assume] })
}
resource "aws_iam_role_policy_attachment" "ebs" {
  role       = aws_iam_role.workload["ebs"].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}
resource "aws_eks_addon" "ebs" {
  cluster_name             = var.cluster_name
  addon_name               = "aws-ebs-csi-driver"
  addon_version            = var.ebs_csi_version
  service_account_role_arn = aws_iam_role.workload["ebs"].arn
  configuration_values = jsonencode({ controller = {
    extraVolumeTags = { component = "source", project = "bravo-platform", managed-by = "kubernetes" }
  } })
  resolve_conflicts_on_create = "NONE"
  resolve_conflicts_on_update = "NONE"
  preserve                    = true
  depends_on                  = [aws_iam_role_policy_attachment.ebs]
}

resource "aws_kms_key" "application" {
  description             = "T6 source application objects and Secrets Manager"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "EnableAccountIAM", Effect = "Allow",
    Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }, Action = "kms:*", Resource = "*" },
    { Sid    = "CloudTrailEncryption", Effect = "Allow", Principal = { Service = "cloudtrail.amazonaws.com" },
      Action = ["kms:GenerateDataKey*", "kms:DescribeKey"], Resource = "*",
    Condition = { StringEquals = { "aws:SourceArn" = local.trail_arn } } }
  ] })
}
resource "aws_kms_alias" "application" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.application.key_id
}
resource "aws_secretsmanager_secret" "application" {
  for_each                = toset(["db", "control", "t14"])
  name                    = "${var.name}/${each.key}"
  kms_key_id              = aws_kms_key.application.arn
  recovery_window_in_days = 7
  # Secret versions are created from memory by seed.py, never Terraform state.
}
# The T14 Secret holds no credential, so its fixed version may live in state (unlike db and control).
# Only the app role may read it, whatever an identity policy grants.
resource "aws_secretsmanager_secret_version" "t14" {
  secret_id     = aws_secretsmanager_secret.application["t14"].id
  secret_string = "bravo t14 canary"
}
resource "aws_secretsmanager_secret_policy" "t14" {
  secret_arn = aws_secretsmanager_secret.application["t14"].arn
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "AppRoleOnly", Effect = "Deny", Principal = "*", Action = "secretsmanager:GetSecretValue", Resource = "*",
    Condition = { ArnNotEquals = { "aws:PrincipalArn" = aws_iam_role.workload["app"].arn } }
  }] })
}
# IAM-01.1 canary: the probe pins this version and expects the app role 200 and anonymous 403.
resource "aws_s3_object" "t14" {
  bucket  = aws_s3_bucket.attachments.id
  key     = "attachments/t14-canary.txt"
  content = "bravo t14 canary\n"
}
resource "aws_s3_bucket" "attachments" {
  bucket        = "${var.name}-attachments-${var.account_id}"
  force_destroy = false
}
resource "aws_s3_bucket" "audit" {
  bucket        = "${var.name}-audit-${var.account_id}"
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "attachments" {
  bucket                  = aws_s3_bucket.attachments.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
moved {
  from = aws_s3_bucket_public_access_block.buckets["attachments"]
  to   = aws_s3_bucket_public_access_block.attachments
}
moved {
  from = aws_s3_bucket_public_access_block.buckets["audit"]
  to   = aws_s3_bucket_public_access_block.audit
}
resource "aws_s3_bucket_ownership_controls" "buckets" {
  for_each = { attachments = aws_s3_bucket.attachments.id, audit = aws_s3_bucket.audit.id }
  bucket   = each.value
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_versioning" "buckets" {
  for_each = { attachments = aws_s3_bucket.attachments.id, audit = aws_s3_bucket.audit.id }
  bucket   = each.value
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "attachments" {
  bucket = aws_s3_bucket.attachments.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.application.arn
    }
  }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.application.arn
    }
  }
}
resource "aws_s3_bucket_policy" "attachments" {
  bucket = aws_s3_bucket.attachments.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [aws_s3_bucket.attachments.arn, "${aws_s3_bucket.attachments.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_s3_bucket_policy" "audit" {
  bucket = aws_s3_bucket.audit.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid      = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
      Resource = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    { Sid    = "CloudTrailAclCheck", Effect = "Allow", Principal = { Service = "cloudtrail.amazonaws.com" },
      Action = "s3:GetBucketAcl", Resource = aws_s3_bucket.audit.arn,
    Condition = { StringEquals = { "aws:SourceArn" = local.trail_arn } } },
    { Sid    = "CloudTrailDelivery", Effect = "Allow", Principal = { Service = "cloudtrail.amazonaws.com" },
      Action = "s3:PutObject", Resource = "${aws_s3_bucket.audit.arn}/AWSLogs/${var.account_id}/*",
      Condition = { StringEquals = { "aws:SourceArn" = local.trail_arn,
    "s3:x-amz-acl" = "bucket-owner-full-control" } } }
  ] })
}
resource "aws_cloudtrail" "source" {
  name                          = var.name
  s3_bucket_name                = aws_s3_bucket.audit.id
  include_global_service_events = true
  is_multi_region_trail         = true
  enable_log_file_validation    = true
  enable_logging                = true
  kms_key_id                    = aws_kms_key.application.arn
  event_selector {
    read_write_type           = "All"
    include_management_events = true
    data_resource {
      type   = "AWS::S3::Object"
      values = ["${aws_s3_bucket.attachments.arn}/"]
    }
  }
  depends_on = [aws_s3_bucket_policy.audit, aws_s3_bucket_public_access_block.audit,
  aws_s3_bucket_server_side_encryption_configuration.audit]
}
resource "aws_iam_role_policy" "app" {
  name = "source-app-only"
  role = aws_iam_role.workload["app"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [for k in ["db", "t14"] : aws_secretsmanager_secret.application[k].arn] },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.attachments.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.attachments.arn}/*" },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.application.arn,
    Condition = { StringEquals = { "kms:ViaService" = ["secretsmanager.${var.region}.amazonaws.com", "s3.${var.region}.amazonaws.com"] } } }
  ] })
}
resource "aws_iam_role_policy" "eso" {
  name = "source-control-only"
  role = aws_iam_role.workload["eso"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"], Resource = aws_secretsmanager_secret.application["control"].arn },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.application.arn,
    Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com" } } }
  ] })
}
output "workload_metadata" {
  sensitive = true
  value = {
    region            = var.region, app_role_arn = aws_iam_role.workload["app"].arn,
    eso_role_arn      = aws_iam_role.workload["eso"].arn,
    db_secret_id      = aws_secretsmanager_secret.application["db"].name,
    control_secret_id = aws_secretsmanager_secret.application["control"].name,
    s3_bucket         = aws_s3_bucket.attachments.id
  }
}
# Identifiers for the operations/controls/run.py registration (aws-object, aws-secret); no credential.
output "t14_registration" {
  sensitive = true
  value = {
    app_role          = { arn = aws_iam_role.workload["app"].arn, role_id = aws_iam_role.workload["app"].unique_id }
    other_role        = { arn = aws_iam_role.t14_other.arn, role_id = aws_iam_role.t14_other.unique_id }
    secret_arn        = aws_secretsmanager_secret.application["t14"].arn
    secret_version_id = aws_secretsmanager_secret_version.t14.version_id
    bucket            = aws_s3_bucket.attachments.id, key = aws_s3_object.t14.key
    object_version_id = aws_s3_object.t14.version_id
  }
}
