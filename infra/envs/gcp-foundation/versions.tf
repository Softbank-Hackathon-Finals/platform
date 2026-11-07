terraform {
  required_version = ">= 1.16.5, < 2.0.0"
  # Local state by default (the backend file names the path outside the repository). For shared state,
  # add an untracked backend_override.tf with `terraform { backend "gcs" {} }` and put bucket/prefix in
  # the private backend file (README).
  backend "local" {}
  required_providers {
    google = { source = "hashicorp/google", version = "~> 8.10" }
  }
}

provider "google" {
  project        = var.project_id
  region         = var.region
  default_labels = local.labels
}
