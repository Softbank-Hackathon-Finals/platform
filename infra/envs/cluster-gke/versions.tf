terraform {
  required_version = ">= 1.16.5, < 2.0.0"
  # Local state by default; GCS through an untracked backend_override.tf (see gcp-foundation README).
  backend "local" {}
  required_providers {
    google = { source = "hashicorp/google", version = "~> 8.10" }
  }
}

provider "google" {
  project        = var.project_id
  default_labels = local.labels
}
