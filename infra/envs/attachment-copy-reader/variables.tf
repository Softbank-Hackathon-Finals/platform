variable "account_id" {
  description = "Exact reviewed AWS account holding the attachment bucket, the application key and the SSO role."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the exact twelve-digit account ID through private input."
  }
}

variable "region" {
  description = "Region of the attachment bucket and its key; the KMS condition names S3 in this region."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.region))
    error_message = "Supply an explicit commercial AWS region; there is no default region."
  }
}

variable "bucket_arn" {
  description = "The source attachment bucket (source-workloads), the only bucket the role may list and read."
  type        = string
  sensitive   = true
  validation {
    # Same shape as attachment_job.source_session_policy accepts.
    condition     = can(regex("^arn:aws:s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_arn))
    error_message = "Supply one exact S3 bucket ARN, without an object path or wildcard."
  }
}

variable "kms_key_arn" {
  description = "The application key encrypting that bucket (source-workloads), usable only through S3."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^arn:aws:kms:${var.region}:${var.account_id}:key/[0-9a-f-]{36}$", var.kms_key_arn))
    error_message = "Supply one exact same-account, same-region KMS key ARN, not an alias or wildcard."
  }
}

variable "trusted_role_arn" {
  description = "Exact existing IAM role of the reviewed BravoDevelopmentAdministrator permission set, including its reserved path and suffix."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:role/aws-reserved/sso[.]amazonaws[.]com/([a-z]{2}-[a-z]+-[0-9]+/)?AWSReservedSSO_BravoDevelopmentAdministrator_[0-9a-f]{16}$", var.trusted_role_arn))
    error_message = "Supply the same-account AWSReservedSSO_BravoDevelopmentAdministrator role ARN with its full reserved path and suffix, not another permission set, user, STS session or wildcard."
  }
}
