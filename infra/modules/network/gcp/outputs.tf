output "network" {
  description = "cluster/gcp inputs. Resource identifiers, not credentials."
  value = {
    project_id         = var.project_id
    region             = var.region
    network            = google_compute_network.cluster.self_link
    subnetwork         = google_compute_subnetwork.cluster.self_link
    pod_range_name     = local.pod_range
    service_range_name = local.service_range
    node_cidr          = var.node_cidr
    labels             = var.labels
  }
}
