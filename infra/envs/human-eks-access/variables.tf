variable "account_id" {
  description = "Exact reviewed AWS account containing both the existing cluster and SSO role."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the exact twelve-digit account ID through private input."
  }
}

variable "region" {
  description = "Explicitly reviewed EKS region; independent of the Identity Center home region."
  type        = string
  validation {
    condition     = can(regex("^[a-z]{2}-[a-z]+-[0-9]+$", var.region))
    error_message = "Supply an explicit commercial AWS region; there is no default region."
  }
}

variable "cluster_name" {
  description = "One existing private EKS cluster; this stack does not create or reconfigure it."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^[0-9A-Za-z][A-Za-z0-9_-]{0,99}$", var.cluster_name))
    error_message = "Supply the exact existing EKS cluster name through private input."
  }
}

variable "sso_role_arn" {
  description = "Exact existing AWSReservedSSO IAM role ARN, including its reserved path and unique suffix."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:role/aws-reserved/sso[.]amazonaws[.]com/([a-z]{2}-[a-z]+-[0-9]+/)?AWSReservedSSO_[A-Za-z0-9+=,.@_-]+_[0-9a-f]{16}$", var.sso_role_arn))
    error_message = "Supply one same-account AWSReservedSSO role ARN with its full reserved path and suffix, not a user, STS session, or wildcard."
  }
}
