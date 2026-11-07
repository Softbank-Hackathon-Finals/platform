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
  default = "bravo-findings-pull"
}

variable "issuer_url" {
  type        = string
  description = "The platform cluster's public ServiceAccount issuer (the same URL as infra/envs/platform-gate-signer)."
  validation {
    condition     = can(regex("^https://[a-z0-9.-]+(/[a-z0-9-]+)*$", var.issuer_url))
    error_message = "An https issuer URL without a trailing slash."
  }
}

variable "service_account" {
  type        = string
  default     = "system:serviceaccount:bravo-security:bravo-findings-pull"
  description = "The only subject that may assume the read role."
  validation {
    condition     = can(regex("^system:serviceaccount:[a-z0-9-]+:[a-z0-9-]+$", var.service_account))
    error_message = "One exact ServiceAccount subject, no wildcard."
  }
}
