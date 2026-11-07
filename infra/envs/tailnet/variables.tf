variable "tailnet" {
  description = "Tailnet the credential belongs to (\"-\" is the credential's own tailnet)."
  type        = string
  default     = "-"
}

variable "identities" {
  description = "infra/tailnet/identities.json entries of this tailnet: environment -> operator issuer."
  type = map(object({
    issuer          = string
    namespace       = optional(string, "tailscale-system")
    service_account = optional(string, "operator")
    tailnet         = optional(string, "default")
  }))
  default = {}

  validation {
    condition     = alltrue([for env, v in var.identities : can(regex("^https://", v.issuer)) && can(regex("^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$", env))])
    error_message = "Each identity needs an https issuer and a lowercase environment name."
  }
}
