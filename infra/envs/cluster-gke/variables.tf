variable "project_id" {
  description = "Reviewed GCP project. Supplied in the private tfvars file only."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Use a GCP project ID."
  }
}

variable "location" {
  type    = string
  default = "asia-northeast3-a"
}

variable "name" {
  description = "Cluster name; a source and a target cluster in one project need different names."
  type        = string
  default     = "bravo-gke"
}

variable "component" {
  type    = string
  default = "cluster-gke"
}

# Wired from the gcp-foundation step by infra/stacks/cluster-gke (vars.from).
variable "network" { type = string }
variable "subnetwork" { type = string }
variable "pod_range_name" { type = string }
variable "service_range_name" { type = string }

variable "machine_type" {
  type    = string
  default = "e2-standard-4"
}

variable "node_count" {
  description = "Demo minimum: 2 x e2-standard-4 (CNPG, ESO, Tailscale operator, cloudflared, Argo CD, app)."
  type        = number
  default     = 2
}

variable "secret_reader" {
  description = "Kubernetes service account that reads Secret Manager through ESO (the ESO controller)."
  type        = object({ namespace = string, name = string })
  default     = { namespace = "external-secrets", name = "external-secrets" }
}

variable "secret_prefix" {
  description = "Only secrets whose ID starts with this prefix are readable by the ESO controller."
  type        = string
  default     = "bravo-"
  validation {
    condition     = can(regex("^[a-z][a-z0-9_-]{1,30}$", var.secret_prefix))
    error_message = "Use a short, non-empty secret ID prefix: never grant every secret in the project."
  }
}
