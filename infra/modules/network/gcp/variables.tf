variable "project_id" {
  description = "GCP project that owns the network. Supplied privately; never committed."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Use a GCP project ID (6-30 lower-case letters, digits or hyphens)."
  }
}

variable "region" {
  type    = string
  default = "asia-northeast3"
}

variable "name" {
  description = "Deployment identifier used as a resource name prefix."
  type        = string
  default     = "bravo-gcp"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.name))
    error_message = "Use 3-31 lower-case letters, digits or hyphens, starting with a letter."
  }
}

variable "labels" {
  description = "Labels passed on to labelable resources downstream. Must include component."
  type        = map(string)
  validation {
    condition     = contains(keys(var.labels), "component")
    error_message = "Every cloud resource carries a component label."
  }
}

variable "node_cidr" {
  description = "Primary range of the cluster subnet (nodes)."
  type        = string
  default     = "10.90.0.0/20"
  validation {
    condition     = can(cidrhost(var.node_cidr, 0)) && can(regex("^10\\.", var.node_cidr))
    error_message = "Use a private range within 10.0.0.0/8."
  }
}

variable "pod_cidr" {
  type    = string
  default = "10.92.0.0/14"
}

variable "service_cidr" {
  type    = string
  default = "10.96.0.0/20"
}
