variable "project_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Use a GCP project ID."
  }
}

variable "location" {
  description = "A zone (zonal cluster, one control plane, cheapest) or a region."
  type        = string
  default     = "asia-northeast3-a"
}

variable "name" {
  type    = string
  default = "bravo-gke"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,38}$", var.name))
    error_message = "Use 3-39 lower-case letters, digits or hyphens, starting with a letter."
  }
}

variable "labels" {
  description = "GCP labels for the cluster, nodes and disks. Must include component."
  type        = map(string)
  validation {
    condition     = contains(keys(var.labels), "component")
    error_message = "Every cloud resource carries a component label."
  }
}

variable "network" {
  description = "VPC self link (network/gcp output network.network)."
  type        = string
}

variable "subnetwork" {
  description = "Subnet self link (network/gcp output network.subnetwork)."
  type        = string
}

variable "pod_range_name" { type = string }
variable "service_range_name" { type = string }

variable "master_cidr" {
  description = "Private /28 for the control plane peering. Must not overlap the VPC ranges."
  type        = string
  default     = "172.16.90.0/28"
}

variable "release_channel" {
  type    = string
  default = "REGULAR"
}

variable "machine_type" {
  description = "One pool runs CNPG, ESO, Tailscale operator, cloudflared and Argo CD."
  type        = string
  default     = "e2-standard-4"
}

variable "node_count" {
  description = "Demo minimum: 2 x e2-standard-4 (CNPG, ESO, Tailscale operator, cloudflared, Argo CD, app)."
  type        = number
  default     = 2
  validation {
    condition     = var.node_count >= 1 && var.node_count <= 3
    error_message = "1 to 3 nodes for the demo."
  }
}

variable "disk_size_gb" {
  type    = number
  default = 50
}

variable "authorized_networks" {
  description = "Private CIDRs allowed to the control plane IP endpoint (kept off by default)."
  type        = list(string)
  default     = []
  validation {
    condition     = !contains(var.authorized_networks, "0.0.0.0/0")
    error_message = "Do not open the control plane to the whole internet."
  }
}
