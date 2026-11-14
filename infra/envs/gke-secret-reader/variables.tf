# Wired from the gcp-gke step's `project_id` output by infra/stacks/cluster-gke.
variable "project_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Use a GCP project ID."
  }
}

# Same reviewed number as gcp-gke (private tfvars): a plan against another project fails before any change.
variable "project_number" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{6,20}$", var.project_number))
    error_message = "project_number must be the numeric GCP project number."
  }
}

variable "component" {
  type    = string
  default = "cluster-gke"
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
