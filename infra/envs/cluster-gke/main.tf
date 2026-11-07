# GKE for either role (A10). The cluster comes from cluster/gcp; this root adds the build side of the
# kind's identity (both.identity): the ESO controller's Kubernetes service account reads Secret Manager
# through Workload Identity, without a Google service account key and only for secrets under a prefix.
locals {
  labels = { project = "bravo-platform", component = var.component, "managed-by" = "terraform" }
}

data "google_project" "this" {
  project_id = var.project_id
}

module "cluster" {
  source             = "../../modules/cluster/gcp"
  project_id         = var.project_id
  location           = var.location
  name               = var.name
  labels             = local.labels
  network            = var.network
  subnetwork         = var.subnetwork
  pod_range_name     = var.pod_range_name
  service_range_name = var.service_range_name
  machine_type       = var.machine_type
  node_count         = var.node_count
}

# Workload Identity Federation for GKE: an IAM principal for one Kubernetes service account.
resource "google_project_iam_member" "eso_secret_reader" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${module.cluster.cluster.workload_pool}/subject/ns/${var.secret_reader.namespace}/sa/${var.secret_reader.name}"
  condition {
    title      = "bravo-secret-prefix"
    expression = "resource.name.startsWith(\"projects/${data.google_project.this.number}/secrets/${var.secret_prefix}\")"
  }
}

output "cluster" {
  description = "get-credentials and the stack's cluster binding (endpoint). No credential."
  value       = merge(module.cluster.cluster, { secret_prefix = var.secret_prefix })
}
