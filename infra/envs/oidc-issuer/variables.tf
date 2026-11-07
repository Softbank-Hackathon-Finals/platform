variable "account_id" {
  description = "Platform AWS account (the provider refuses any other)."
  type        = string
}

variable "region" {
  description = "Region of the issuer bucket; part of every issuer URL, so it never changes once published."
  type        = string
}

variable "bucket_name" {
  description = "Issuer bucket. Part of every issuer URL: renaming it breaks every cluster that uses it."
  type        = string
  default     = "bravo-oidc-issuer"
}

variable "issuers_dir" {
  description = "Directory of <env>/jwks.json (default: issuers/ next to this file)."
  type        = string
  default     = null
}
