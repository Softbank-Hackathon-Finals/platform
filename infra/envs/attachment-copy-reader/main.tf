# T8 attachment copy: one role an SSO operator assumes to hand the copy Job a short S3 session.
# An SSO session cannot call GetFederationToken. The bucket, its key and the SSO role belong to other
# states; this one only reads the account and the SSO role, and owns the role and its inline policy.
data "aws_caller_identity" "current" {}
data "aws_iam_role" "trusted" { name = reverse(split("/", var.trusted_role_arn))[0] }

locals {
  objects = "${var.bucket_arn}/*"
  # Must stay equal to attachment_job.source_session_policy. tests/session-policy.json holds the
  # expected document; this stack's tests and scripts/tests/test_attachment_job.py both compare against it.
  read_policy = { Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "s3:ListBucket", Resource = var.bucket_arn },
    { Effect = "Allow", Action = "s3:GetObject", Resource = local.objects },
    { Effect = "Allow", Action = "kms:Decrypt", Resource = var.kms_key_arn,
    Condition = { StringEquals = { "kms:ViaService" = "s3.${var.region}.amazonaws.com" } } },
    # Explicit denials also constrain anything a resource policy or a later attachment might grant.
    { Effect = "Deny", NotAction = ["s3:ListBucket", "s3:GetObject", "kms:Decrypt"], Resource = "*" },
    { Effect = "Deny", Action = ["s3:ListBucket", "s3:GetObject"], NotResource = [var.bucket_arn, local.objects] },
  ] }
}

resource "aws_iam_role" "reader" {
  name        = "bravo-attachment-copy-reader"
  description = "T8 attachment copy: read the source attachment bucket for one Job session"
  # Assuming it from an SSO session is role chaining, which AWS caps at one hour anyway.
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { AWS = var.trusted_role_arn }, Action = "sts:AssumeRole"
  }] })

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.account_id
      error_message = "The authenticated caller must belong to the reviewed account."
    }
    precondition {
      condition     = data.aws_iam_role.trusted.arn == var.trusted_role_arn
      error_message = "IAM must return the exact reviewed SSO role ARN, including its reserved path and suffix."
    }
  }
}

resource "aws_iam_role_policy" "reader" {
  name   = "t8-attachment-copy-read"
  role   = aws_iam_role.reader.name
  policy = jsonencode(local.read_policy)
}

output "reader_role_arn" {
  description = "The --role-arn for aws sts assume-role in docs/operations/t8-attachment-copy-job.md step 5."
  value       = aws_iam_role.reader.arn
  sensitive   = true
}
