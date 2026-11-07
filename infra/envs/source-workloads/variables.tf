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
  default = "bravo-source-workloads"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.name))
    error_message = "Use a short lowercase workload resource prefix."
  }
}
variable "ebs_csi_version" {
  type    = string
  default = "v1.67.2-eksbuild.1"
}
variable "t14_operator_role_arn" {
  description = "Exact existing IAM role of the BravoDevelopmentAdministrator permission set that runs T14 operations/controls/run.py."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^arn:aws:iam::${var.account_id}:role/aws-reserved/sso[.]amazonaws[.]com/([a-z]{2}-[a-z]+-[0-9]+/)?AWSReservedSSO_BravoDevelopmentAdministrator_[0-9a-f]{16}$", var.t14_operator_role_arn))
    error_message = "Supply the same-account AWSReservedSSO_BravoDevelopmentAdministrator role ARN with its full reserved path and suffix, not another permission set, user, STS session or wildcard."
  }
}
