variable "account_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "An exact target AWS account is required."
  }
}
variable "region" {
  type    = string
  default = "ap-northeast-2"
}
variable "cluster_name" {
  type    = string
  default = "bravo-source-runtime"
}
variable "name" {
  type    = string
  default = "bravo-source-backup"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.name))
    error_message = "Use a short lowercase resource prefix."
  }
}
variable "prefix" {
  description = "The only object prefix the Barman roles may use. Barman adds the server name below it."
  type        = string
  default     = "t35/source-db/"
  validation {
    condition     = can(regex("^[a-z0-9-]+(/[a-z0-9-]+)*/$", var.prefix))
    error_message = "Use a non-root prefix ending in a slash."
  }
}
variable "writer_service_account" {
  description = "CNPG runs the instances and the Barman sidecar under a ServiceAccount named after the Cluster (T6 source-dev/source-db)."
  type        = object({ namespace = string, name = string })
  default     = { namespace = "source-dev", name = "source-db" }
}
variable "reader_service_account" {
  description = "The isolated restore Cluster's ServiceAccount. It must never be the writer's."
  type        = object({ namespace = string, name = string })
  default     = { namespace = "t35-restore-check", name = "source-db-restore" }
  validation {
    condition     = var.reader_service_account.namespace != var.writer_service_account.namespace
    error_message = "Restore checks run in their own namespace, away from the source workloads."
  }
}
variable "custodian_role_arns" {
  description = "Exact IAM role ARNs (IAM Identity Center roles keep their path) that may delete backups or change the bucket's protection. Everyone else is denied."
  type        = list(string)
  validation {
    condition = length(var.custodian_role_arns) >= 1 && length(var.custodian_role_arns) <= 5 && alltrue([
      for arn in var.custodian_role_arns : can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$", arn))
    ])
    error_message = "List one to five exact role ARNs without wildcards."
  }
}
variable "noncurrent_version_days" {
  description = "Overwritten or deleted versions are kept this long. Barman never overwrites WAL or base backup files."
  type        = number
  default     = 7
  validation {
    condition     = var.noncurrent_version_days >= 1 && var.noncurrent_version_days <= 30
    error_message = "Keep previous versions for 1 to 30 days."
  }
}
