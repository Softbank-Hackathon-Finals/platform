variable "project_id" {
  type = string
}

# Same role as allowed_account_ids on AWS: a plan against another project fails before any change.
variable "project_number" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{6,20}$", var.project_number))
    error_message = "project_number must be the numeric GCP project number."
  }
}

variable "region" {
  type    = string
  default = "asia-northeast3"
}

variable "zone" {
  type    = string
  default = "asia-northeast3-a"
}

# Prefix for every resource. Kept apart from the A10 GKE names (bravo-gcp, bravo-gke).
variable "name" {
  type    = string
  default = "bravo-dest-gke"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.name)) && !startswith(var.name, "bravo-gcp") && !startswith(var.name, "bravo-gke")
    error_message = "name must be a short lowercase prefix and must not reuse the bravo-gcp/bravo-gke names."
  }
}

# Disjoint from gcp-rke2 (10.64.0.0/24) and A10 (10.90.0.0/20).
variable "node_cidr" {
  type    = string
  default = "10.65.0.0/24"
}

variable "pod_cidr" {
  type    = string
  default = "10.66.0.0/16"
}

variable "service_cidr" {
  type    = string
  default = "10.67.0.0/20"
}

variable "machine_type" {
  type    = string
  default = "e2-standard-4"
}

variable "node_count" {
  type    = number
  default = 2
}

# STABLE tracks Kubernetes 1.35, the minor the EKS source and the RKE2 destinations run.
variable "release_channel" {
  type    = string
  default = "STABLE"
}
