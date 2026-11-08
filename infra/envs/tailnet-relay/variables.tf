variable "account_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the reviewed deployment account privately."
  }
}
variable "region" { default = "ap-northeast-2" }
variable "name" { default = "bravo-peer-relay" }
variable "ami_id" {
  description = "Exact Ubuntu 24.04 arm64 image, resolved from Canonical's public SSM parameter."
  type        = string
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "Supply an exact AMI ID."
  }
}
variable "instance_type" {
  description = "One small arm64 host. The relay only forwards UDP; tailscaled is its only workload."
  type        = string
  default     = "t4g.micro"
}
variable "relay_port" {
  description = "The UDP port the relay server binds, advertises and the security group opens. Not tailscaled's own 41641."
  type        = number
  default     = 40000
  validation {
    condition     = var.relay_port >= 1024 && var.relay_port <= 65535 && var.relay_port != 41641
    error_message = "Use one unprivileged UDP port."
  }
}
variable "tailscale_tag" {
  description = "Tailnet identity of the relay. infra/iac/environments/platform.json (tailnet-peer-relay) grants its use."
  type        = string
  default     = "tag:bravo-peer-relay"
  validation {
    condition     = can(regex("^tag:bravo-[a-z0-9-]+$", var.tailscale_tag))
    error_message = "Use one tag:bravo-* tag. Auth keys never belong in Terraform inputs."
  }
}
variable "tags" {
  description = "Cost attribution tags. infra/envs/cost-aws attributes shared tooling to component=platform."
  type        = map(string)
  default     = { project = "bravo-platform", component = "platform", purpose = "tailnet-relay", "managed-by" = "terraform" }
  validation {
    condition     = lookup(var.tags, "project", "") == "bravo-platform" && lookup(var.tags, "component", "") == "platform"
    error_message = "Keep project=bravo-platform and component=platform: other values appear as unattributed cost in scripts/cost_report.py."
  }
}
