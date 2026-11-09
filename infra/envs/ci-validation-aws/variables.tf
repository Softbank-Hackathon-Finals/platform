variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Use the 12-digit owner AWS account ID."
  }
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
  validation {
    condition     = var.region == "ap-northeast-2"
    error_message = "This bounded validation root is for Seoul only."
  }
}

variable "ami_id" {
  type        = string
  description = "Verified Canonical Ubuntu 24.04 arm64 AMI; record the resolved public SSM parameter value."
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "Supply a verified arm64 Ubuntu AMI ID."
  }
}

variable "deployment" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{2,30}$", var.deployment))
    error_message = "Use a unique, short lowercase deployment identifier."
  }
}

variable "ttl_minutes" {
  type    = number
  default = 90
  validation {
    condition     = var.ttl_minutes >= 15 && var.ttl_minutes <= 90 && floor(var.ttl_minutes) == var.ttl_minutes
    error_message = "The host lifetime must be 15 to 90 whole minutes."
  }
}

variable "expires_at" {
  type        = string
  description = "Operator-declared expiry in RFC3339; the guest shutdown timer enforces ttl_minutes."
  validation {
    condition     = can(timecmp(var.expires_at, var.expires_at))
    error_message = "Use an RFC3339 expiry timestamp."
  }
}
