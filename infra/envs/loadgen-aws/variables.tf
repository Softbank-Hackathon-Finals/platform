variable "account_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the reviewed deployment account privately."
  }
}
variable "region" { default = "ap-northeast-2" }
variable "name" { default = "bravo-loadgen" }
variable "ami_id" {
  description = "Exact Ubuntu 24.04 amd64 image, resolved from Canonical's public SSM parameter."
  type        = string
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "Supply an exact AMI ID."
  }
}
variable "instance_type" {
  description = "One x86_64 host with at least 2 vCPUs and 2 GiB. The floor's basis is in README.md."
  type        = string
  default     = "t3.small"
}
variable "tailscale_tag" {
  description = "Tailnet identity of this host. The platform ACL allows window delivery from this tag only."
  type        = string
  validation {
    condition     = can(regex("^tag:bravo-[a-z0-9-]+$", var.tailscale_tag))
    error_message = "Use one tag:bravo-* tag. Auth keys never belong in Terraform inputs."
  }
}
variable "source_user_pool_arn" {
  description = "Only when the measured app requires sign-in: source-identity's user_pool_arn output. The host may then call AdminInitiateAuth on that pool and nothing else."
  type        = string
  default     = null
  validation {
    condition     = var.source_user_pool_arn == null || can(regex("^arn:aws:cognito-idp:[a-z0-9-]+:[0-9]{12}:userpool/[a-z0-9-]+_[A-Za-z0-9]+$", var.source_user_pool_arn))
    error_message = "Use the exact user pool ARN from source-identity, not a wildcard."
  }
}
variable "tags" {
  description = "Cost attribution tags. infra/envs/cost-aws attributes shared tooling to component=platform."
  type        = map(string)
  default     = { project = "bravo-platform", component = "platform", purpose = "load-generator", "managed-by" = "terraform" }
  validation {
    condition     = lookup(var.tags, "project", "") == "bravo-platform" && lookup(var.tags, "component", "") == "platform"
    error_message = "Keep project=bravo-platform and component=platform: other values appear as unattributed cost in scripts/cost_report.py."
  }
}
