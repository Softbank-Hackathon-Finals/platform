variable "account_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "An exact platform AWS account is required."
  }
}
variable "region" {
  type    = string
  default = "ap-northeast-2"
}
variable "name" {
  type    = string
  default = "bravo-platform-audit"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.name))
    error_message = "Use a short lowercase resource prefix."
  }
}
variable "prefix" {
  description = "The only object prefix the writer and reader roles may use. The sink writes audit/<plan>/<run>/<seq>.json below it."
  type        = string
  default     = "audit/"
  validation {
    condition     = can(regex("^[a-z0-9-]+(/[a-z0-9-]+)*/$", var.prefix))
    error_message = "Use a non-root prefix ending in a slash."
  }
}
variable "retention_days" {
  description = "Default COMPLIANCE retention. The demo uses 1 day so the stack can be torn down the day after; production follows the regulation (years)."
  type        = number
  default     = 1
  validation {
    condition     = var.retention_days >= 1 && var.retention_days <= 3650
    error_message = "Retain for 1 to 3650 days."
  }
}
variable "custodian_role_arns" {
  description = "Exact IAM role ARNs (IAM Identity Center roles keep their path) that may change the bucket's protection or remove expired records. Everyone else is denied."
  type        = list(string)
  validation {
    condition = length(var.custodian_role_arns) >= 1 && length(var.custodian_role_arns) <= 5 && alltrue([
      for arn in var.custodian_role_arns : can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$", arn))
    ])
    error_message = "List one to five exact role ARNs without wildcards."
  }
}
variable "writer_principal_arns" {
  description = "Exact IAM role ARNs allowed to assume the writer role (the T10 runtime role, and an operator role for verification)."
  type        = list(string)
  validation {
    condition = length(var.writer_principal_arns) >= 1 && length(var.writer_principal_arns) <= 5 && alltrue([
      for arn in var.writer_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$", arn))
    ])
    error_message = "List one to five exact role ARNs without wildcards."
  }
}
variable "reader_principal_arns" {
  description = "Exact IAM role ARNs allowed to assume the read-only search role (console, operators)."
  type        = list(string)
  validation {
    condition = length(var.reader_principal_arns) >= 1 && length(var.reader_principal_arns) <= 5 && alltrue([
      for arn in var.reader_principal_arns : can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$", arn))
    ])
    error_message = "List one to five exact role ARNs without wildcards."
  }
}
