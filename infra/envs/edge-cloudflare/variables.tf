variable "account_id" {
  description = "Cloudflare account that owns the tunnels, pools and monitor."
  type        = string
}

variable "zone_id" {
  description = "Zone that serves every hostname below."
  type        = string
}

variable "tunnels" {
  description = "Existing remotely managed tunnel IDs. Tunnels and their tokens stay outside Terraform (edge-tunnels/README.md)."
  type        = object({ source = string, target = string })
}

variable "hostnames" {
  description = "app is the load-balanced demo-chat address; source/target are each tunnel's own origin host; login is the destination Keycloak (null keeps it closed)."
  type = object({
    app    = string
    source = string
    target = string
    login  = optional(string)
  })
  validation {
    condition = length(distinct(compact([
      var.hostnames.app, var.hostnames.source, var.hostnames.target, var.hostnames.login
    ]))) == length(compact([var.hostnames.app, var.hostnames.source, var.hostnames.target, var.hostnames.login]))
    error_message = "Every hostname is different: the login address must not share the weighted app address."
  }
}

variable "adopt" {
  description = "IDs of resources created before this stack (private tfvars). Imported once; leave empty for a fresh zone."
  type = object({
    configs = optional(bool, false)
    monitor = optional(string)
    lb      = optional(string)
    pools   = optional(map(string), {})
    dns     = optional(map(string), {})
  })
  default = {}
}
