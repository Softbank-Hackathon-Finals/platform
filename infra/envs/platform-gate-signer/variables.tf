variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "account_id" {
  type        = string
  description = "The platform AWS account; the provider refuses any other."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "A 12-digit AWS account id."
  }
}

variable "name" {
  type    = string
  default = "bravo-gate-signer"
}

variable "issuer_url" {
  type        = string
  description = "The platform cluster's public ServiceAccount issuer (infra/envs/oidc-issuer output for env platform)."
  validation {
    condition     = can(regex("^https://[a-z0-9.-]+(/[a-z0-9-]+)*$", var.issuer_url))
    error_message = "An https issuer URL without a trailing slash."
  }
}

variable "service_account" {
  type        = string
  default     = "system:serviceaccount:platform-state:t10-workflow-host"
  description = "The only subject that may assume the signer role."
  validation {
    condition     = can(regex("^system:serviceaccount:[a-z0-9-]+:[a-z0-9-]+$", var.service_account))
    error_message = "One exact ServiceAccount subject, no wildcard."
  }
}

variable "vpc_id" {
  type        = string
  description = "The platform VPC (aws-foundation); the STS and KMS interface endpoints live here."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Platform private subnets for the endpoint ENIs."
  validation {
    condition     = length(var.subnet_ids) >= 1 && length(var.subnet_ids) <= 3
    error_message = "One to three private subnets."
  }
}

variable "client_security_group_id" {
  type        = string
  description = "The platform node security group; pod traffic reaches the endpoints from the node."
}
