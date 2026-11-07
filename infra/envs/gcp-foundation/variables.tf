variable "project_id" {
  description = "Reviewed GCP project (billing linked). Supplied in the private tfvars file only."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Use a GCP project ID."
  }
}

variable "region" {
  type    = string
  default = "asia-northeast3"
}

variable "name" {
  description = "Resource name prefix of this foundation."
  type        = string
  default     = "bravo-gcp"
}

variable "component" {
  description = "component label of every labelable resource (cost and teardown)."
  type        = string
  default     = "cluster-gke"
}

variable "node_cidr" {
  type    = string
  default = "10.90.0.0/20"
}

variable "pod_cidr" {
  type    = string
  default = "10.92.0.0/14"
}

variable "service_cidr" {
  type    = string
  default = "10.96.0.0/20"
}
