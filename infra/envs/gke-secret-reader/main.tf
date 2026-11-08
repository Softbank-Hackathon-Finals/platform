# A10 (ADR-0034 both.identity, build side) on a GKE cluster from T50's gcp-gke: the ESO controller's
# Kubernetes service account reads Secret Manager through Workload Identity, without a Google service
# account key and only for secrets under a prefix. The cluster, network and node identity stay in gcp-gke.
data "google_project" "this" {}

resource "google_project_service" "secret_manager" {
  service            = "secretmanager.googleapis.com"
  disable_on_destroy = false # never switch an API off under other users of the project
  lifecycle {
    precondition {
      condition     = data.google_project.this.number == var.project_number
      error_message = "The provider project does not match the reviewed project_number."
    }
  }
}

# Workload Identity Federation for GKE: an IAM principal for one Kubernetes service account. The pool is
# the project's (gcp-gke workload_identity_config), so the grant covers that service account in every
# GKE cluster of the project; the stack builds one cluster per side and names it apart (bravo-<side>-gke).
resource "google_project_iam_member" "eso_secret_reader" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "principal://iam.googleapis.com/projects/${var.project_number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${var.secret_reader.namespace}/sa/${var.secret_reader.name}"
  condition {
    title      = "bravo-secret-prefix"
    expression = "resource.name.startsWith(\"projects/${var.project_number}/secrets/${var.secret_prefix}\")"
  }
  depends_on = [google_project_service.secret_manager]
}

output "secret_reader" {
  description = "The principal ESO authenticates as and the secret prefix it may read. No credential."
  value       = { member = google_project_iam_member.eso_secret_reader.member, secret_prefix = var.secret_prefix }
}
