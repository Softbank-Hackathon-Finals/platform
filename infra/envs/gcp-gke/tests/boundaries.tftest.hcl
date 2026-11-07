mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "1000001" }
  }
}

variables {
  project_id     = "bravo-test-project"
  project_number = "1000001"
}

run "private_cluster_iam_endpoint" {
  command = apply
  assert {
    condition     = google_container_cluster.this.private_cluster_config[0].enable_private_nodes && google_container_cluster.this.private_cluster_config[0].enable_private_endpoint
    error_message = "Nodes and the IP endpoint must be private."
  }
  assert {
    condition     = google_container_cluster.this.control_plane_endpoints_config[0].dns_endpoint_config[0].allow_external_traffic && !google_container_cluster.this.master_authorized_networks_config[0].gcp_public_cidrs_access_enabled
    error_message = "Operators come in through the IAM-checked DNS endpoint only."
  }
  assert {
    condition     = google_container_node_pool.default.node_config[0].workload_metadata_config[0].mode == "GKE_METADATA" && google_container_cluster.this.workload_identity_config[0].workload_pool == "bravo-test-project.svc.id.goog"
    error_message = "Pods must not see the node service account token."
  }
  assert {
    condition     = google_container_cluster.this.datapath_provider == "ADVANCED_DATAPATH"
    error_message = "NetworkPolicy must be enforced (Dataplane V2)."
  }
  assert {
    condition     = google_artifact_registry_repository_iam_member.node_read.role == "roles/artifactregistry.reader" && google_project_iam_member.node_default.role == "roles/container.defaultNodeServiceAccount"
    error_message = "The node account gets the GKE minimum and read on its own repository only."
  }
  assert {
    condition     = output.issuer == "https://container.googleapis.com/v1/projects/bravo-test-project/locations/asia-northeast3-a/clusters/bravo-dest-gke"
    error_message = "The issuer output must be the cluster's public OIDC issuer."
  }
}

run "other_project_is_refused" {
  command = plan
  variables {
    project_number = "1000002"
  }
  expect_failures = [google_compute_network.this]
}

run "a10_names_are_refused" {
  command = plan
  variables {
    name = "bravo-gke-dest"
  }
  expect_failures = [var.name]
}
