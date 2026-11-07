variable "project_id" {
  type = string
}

# Same role as allowed_account_ids on AWS: a plan against another project fails before any change.
variable "project_number" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{6,20}$", var.project_number))
    error_message = "project_number must be the numeric GCP project number."
  }
}

# The platform cluster's public ServiceAccount issuer (oidc-issuer env platform), the same one the
# AWS GuardDuty reader trusts (infra/envs/platform-security).
variable "issuer_url" {
  type = string
  validation {
    condition     = can(regex("^https://[^/]+(/[A-Za-z0-9._-]+)*$", var.issuer_url))
    error_message = "issuer_url must be an https issuer without a trailing slash."
  }
}

# The only token subject the pool accepts and the reader account lets impersonate.
variable "service_account" {
  type    = string
  default = "system:serviceaccount:bravo-security:bravo-findings-scc-pull"
  validation {
    condition     = can(regex("^system:serviceaccount:[a-z0-9-]+:[a-z0-9-]+$", var.service_account))
    error_message = "One ServiceAccount subject, no wildcard."
  }
}

# The environment the findings API records these findings under (BRAVO_SCC_SCOPE_FILE key).
variable "environment_id" {
  type    = string
  default = "env-gcp"
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$", var.environment_id))
    error_message = "environment_id must match the findings API environment id."
  }
}

# Where the collector Pod mounts its projected token (credential configuration output).
variable "token_path" {
  type    = string
  default = "/var/run/secrets/gcp/token"
}

variable "pool_id" {
  type    = string
  default = "bravo-platform"
}

variable "provider_id" {
  type    = string
  default = "platform-oidc"
}

variable "reader_id" {
  type    = string
  default = "bravo-scc-reader"
}
