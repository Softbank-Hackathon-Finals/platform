terraform {
  required_version = ">= 1.16.5, < 2.0.0"
  # Partial GCS backend like gcp-gke: the bucket and prefix stay in a private -backend-config file.
  backend "gcs" {}
  required_providers {
    google = { source = "hashicorp/google", version = "~> 8.8.1" }
  }
}

provider "google" {
  project = var.project_id
  default_labels = {
    project      = "bravo-platform"
    component    = var.component
    "managed-by" = "terraform"
  }
}
