variable "account_id" {
  description = "Expected AWS account; prevents apply in a different operator account."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be the expected 12-digit AWS account ID."
  }
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "name" {
  description = "Deployment identifier, not a product name. Use a unique value for validation."
  type        = string
  default     = "bravo-foundation"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.name))
    error_message = "Use 3-40 lower-case letters, digits or hyphens, starting with a letter."
  }
}

variable "owner" {
  type    = string
  default = "team-bravo"
}

variable "availability_zones" {
  description = "Two distinct AZs in region. Source EKS needs subnets in at least two AZs."
  type        = list(string)
  default     = ["ap-northeast-2a", "ap-northeast-2c"]
  validation {
    condition = length(var.availability_zones) == 2 && length(toset(var.availability_zones)) == 2 && alltrue([
      for zone in var.availability_zones : can(regex("^${var.region}[a-z]$", zone))
    ])
    error_message = "Choose two distinct availability zones within region."
  }
}

variable "network_cidr" {
  description = "Private /15 split into source and platform /16s. Must not overlap other connected networks."
  type        = string
  default     = "10.80.0.0/15"
  validation {
    condition = can(cidrsubnet(var.network_cidr, 1, 1)) && can(regex("^10\\.", var.network_cidr)) && try(
      tonumber(split("/", var.network_cidr)[1]) == 15 && cidrhost(var.network_cidr, 0) == split("/", var.network_cidr)[0], false
    )
    error_message = "Use an aligned /15 within 10.0.0.0/8, for example 10.80.0.0/15."
  }
}
