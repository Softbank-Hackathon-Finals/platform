variable "account_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the reviewed deployment account privately."
  }
}
variable "region" { default = "ap-northeast-2" }
variable "name" { default = "bravo-demo-chat" }
variable "domain_prefix" {
  description = "Cognito managed login domain prefix. Globally unique per region; it becomes https://<prefix>.auth.<region>.amazoncognito.com."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9](-?[a-z0-9])*$", var.domain_prefix)) && length(var.domain_prefix) <= 63 && !can(regex("(aws|amazon|cognito)", var.domain_prefix))
    error_message = "Use lowercase letters, digits and single hyphens; AWS refuses prefixes containing aws, amazon or cognito."
  }
}
variable "app_origins" {
  description = "Public origins of the demo app. The app's sign-in callback is <origin>/. Each environment behind the same Edge hostname shares one origin."
  type        = list(string)
  validation {
    condition = length(var.app_origins) > 0 && alltrue([for o in var.app_origins :
      can(regex("^https://[a-z0-9.-]+(:[0-9]+)?$", o)) ||
    (var.allow_local_callbacks && can(regex("^http://(localhost|127\\.0\\.0\\.1)(:[0-9]+)?$", o)))])
    error_message = "Origins are https://host[:port] without a path. Plain http localhost needs allow_local_callbacks = true."
  }
}
variable "allow_local_callbacks" {
  description = "Only for a throwaway pool on this machine. A demo pool must not keep localhost callbacks."
  type        = bool
  default     = false
}
variable "id_token_minutes" {
  description = "ID token lifetime. The app only accepts ID tokens; a shorter life narrows a leaked token."
  type        = number
  default     = 60
  validation {
    condition     = var.id_token_minutes >= 5 && var.id_token_minutes <= 1440
    error_message = "Cognito accepts 5 minutes to 1 day."
  }
}
variable "tags" {
  description = "Cost attribution. The user pool belongs to the migrated workload, so it is component=source."
  type        = map(string)
  default     = { project = "bravo-platform", component = "source", purpose = "demo-app-identity", "managed-by" = "terraform" }
  validation {
    condition     = lookup(var.tags, "project", "") == "bravo-platform" && lookup(var.tags, "component", "") == "source"
    error_message = "Keep project=bravo-platform and component=source so scripts/cost_report.py attributes the pool to the source."
  }
}
