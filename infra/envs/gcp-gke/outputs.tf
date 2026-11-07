output "cluster" {
  value = google_container_cluster.this.name
}

output "location" {
  value = google_container_cluster.this.location
}

output "project_id" {
  value = var.project_id
}

output "dns_endpoint" {
  value = google_container_cluster.this.control_plane_endpoints_config[0].dns_endpoint_config[0].endpoint
}

# Public OIDC issuer of the cluster's ServiceAccount tokens: the issuer of the Tailscale operator's
# federated identity (ADR-0035) and of GCP Workload Identity.
output "issuer" {
  value = "https://container.googleapis.com/v1/projects/${var.project_id}/locations/${google_container_cluster.this.location}/clusters/${google_container_cluster.this.name}"
}

output "registry" {
  value = "${local.registry}/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
