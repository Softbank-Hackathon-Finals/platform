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
  default = "bravo-rke2"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.name)) && !startswith(var.name, "bravo-gcp") && !startswith(var.name, "bravo-gke")
    error_message = "name must be a short lowercase prefix and must not reuse the bravo-gcp/bravo-gke names."
  }
}

variable "subnet_cidr" {
  type    = string
  default = "10.64.0.0/24"
}

variable "machine_type" {
  type    = string
  default = "e2-standard-8"
}

variable "disk_gb" {
  type    = number
  default = 100
}

# Ubuntu 24.04 amd64, the same release line as the T2 on-prem and T7 AWS hosts.
variable "image" {
  type    = string
  default = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
}

variable "rke2_version" {
  type    = string
  default = "v1.35.9+rke2r1"
  validation {
    condition     = can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+\\+rke2r[0-9]+$", var.rke2_version))
    error_message = "rke2_version must be an exact RKE2 release."
  }
}

# ADR-0035: public OIDC issuer (infra/envs/oidc-issuer, https://<bucket>.s3.<region>.amazonaws.com/<env>) so
# Tailscale can verify the operator's ServiceAccount token. Unset keeps the cluster-internal issuer only.
variable "public_issuer" {
  type    = string
  default = null
  validation {
    condition     = var.public_issuer == null || can(regex("^https://[a-z0-9.-]+(/[a-z0-9-]+)+$", var.public_issuer))
    error_message = "public_issuer must be an https issuer URL without a trailing slash."
  }
}
