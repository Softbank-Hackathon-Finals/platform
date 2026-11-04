variable "account_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "The reviewed target account is required."
  }
}
variable "region" {
  type    = string
  default = "ap-northeast-2"
  validation {
    condition     = var.region == "ap-northeast-2"
    error_message = "This development registry is in the verified Seoul region."
  }
}
variable "github_owner_id" {
  type = string
  validation {
    condition     = can(regex("^[1-9][0-9]+$", var.github_owner_id))
    error_message = "Use the verified immutable GitHub organization ID."
  }
}
variable "github_repository_id" {
  type = string
  validation {
    condition     = can(regex("^[1-9][0-9]+$", var.github_repository_id))
    error_message = "Use the verified immutable demo-chat repository ID."
  }
}
