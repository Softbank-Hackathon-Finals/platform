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
