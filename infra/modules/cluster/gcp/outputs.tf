output "cluster" {
  description = "Access information for get-credentials and the stack's cluster binding. No credential."
  value = {
    name          = google_container_cluster.this.name
    location      = google_container_cluster.this.location
    project_id    = var.project_id
    endpoint      = google_container_cluster.this.control_plane_endpoints_config[0].dns_endpoint_config[0].endpoint
    workload_pool = local.workload_pool
    node_pool     = google_container_node_pool.base.name
  }
}
