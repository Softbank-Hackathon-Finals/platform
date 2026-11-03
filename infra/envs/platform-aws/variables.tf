variable "account_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the reviewed deployment account privately."
  }
}
variable "region" { default = "ap-northeast-2" }
variable "name" { default = "bravo-platform-runtime" }
variable "vpc_id" { type = string }
variable "private_subnet_id" { type = string }
variable "private_route_table_id" { type = string }
variable "egress_subnet_cidr" { default = "10.81.254.0/24" }
variable "ami_id" { type = string }
variable "instance_type" { default = "t3.large" }
variable "rke2_version" {
  default = "v1.35.9+rke2r1"
  validation {
    condition     = can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+\\+rke2r[0-9]+$", var.rke2_version))
    error_message = "Pin an exact RKE2 release."
  }
}
variable "github_oidc_provider_arn" {
  type      = string
  sensitive = true
}
variable "github_oidc_subjects" {
  description = "Exact verified GitHub subjects, including immutable IDs when enabled. No wildcard trust."
  type        = set(string)
  validation {
    condition = length(var.github_oidc_subjects) > 0 && alltrue([
      for subject in var.github_oidc_subjects :
      can(regex("^repo:Softbank-Hackathon-Finals(@[0-9]+)?/platform(@[0-9]+)?:ref:refs/heads/(develop|feature/T7-rke2-oidc)$", subject))
    ])
    error_message = "Only exact platform develop or this validation branch subjects are allowed."
  }
}
