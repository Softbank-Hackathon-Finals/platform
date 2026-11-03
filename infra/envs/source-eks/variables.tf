variable "account_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the reviewed deployment account privately."
  }
}
variable "region" { default = "ap-northeast-2" }
variable "name" { default = "bravo-source-runtime" }
variable "vpc_id" { type = string }
variable "private_subnet_ids" {
  type = list(string)
  validation {
    condition     = length(var.private_subnet_ids) == 2 && length(toset(var.private_subnet_ids)) == 2
    error_message = "Supply two distinct source private subnets."
  }
}
variable "private_route_table_id" { type = string }
variable "egress_subnet_cidr" { default = "10.80.254.0/24" }
variable "platform_vpc_cidr" { default = "10.81.0.0/16" }
variable "kubernetes_version" {
  default = "1.35"
  validation {
    condition     = var.kubernetes_version == "1.35"
    error_message = "This stack is validated against EKS 1.35; review a version change first."
  }
}
variable "node_release_version" {
  description = "Exact EKS AL2023 release selected from AWS SSM, recorded in the private run inputs."
  type        = string
}
variable "instance_type" { default = "t3.large" }
variable "operator_principal_arn" {
  description = "Individual operator IAM role (SSO preferred) or existing personal IAM user for bootstrap validation only. Never an STS session ARN."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/[^*?]+$", var.operator_principal_arn))
    error_message = "Use a stable individual IAM principal ARN, not a shared key or session ARN."
  }
}

variable "vpc_cni_version" {
  description = "Exact AWS-supported vpc-cni add-on version for the selected Kubernetes version."
  type        = string
}
