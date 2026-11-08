locals {
  registry      = "${var.region}-docker.pkg.dev"
  workload_pool = "${var.project_id}.svc.id.goog"
}

data "google_project" "this" {}

resource "google_compute_network" "this" {
  name                    = var.name
  auto_create_subnetworks = false
  lifecycle {
    precondition {
      condition     = data.google_project.this.number == var.project_number
      error_message = "The provider project does not match the reviewed project_number."
    }
  }
}

resource "google_compute_subnetwork" "this" {
  name                     = "${var.name}-${var.region}"
  network                  = google_compute_network.this.id
  ip_cidr_range            = var.node_cidr
  private_ip_google_access = true
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pod_cidr
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.service_cidr
  }
}

# Egress only. The VPC has no ingress firewall rule: nodes have no external address and nothing
# outside the VPC reaches them.
resource "google_compute_router" "this" {
  name    = var.name
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  name                               = var.name
  router                             = google_compute_router.this.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_service_account" "node" {
  account_id   = "${var.name}-node"
  display_name = "${var.name} GKE node"
}

# GKE's documented minimum for a custom node service account (logs, metrics, node bootstrap).
resource "google_project_iam_member" "node_default" {
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.node.email}"
}

resource "google_artifact_registry_repository" "images" {
  repository_id = var.name
  location      = var.region
  format        = "DOCKER"
}

resource "google_artifact_registry_repository_iam_member" "node_read" {
  repository = google_artifact_registry_repository.images.name
  location   = google_artifact_registry_repository.images.location
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.node.email}"
}

resource "google_container_cluster" "this" {
  name                     = var.name
  location                 = var.zone
  network                  = google_compute_network.this.id
  subnetwork               = google_compute_subnetwork.this.id
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false               # spun up and down like gcp-rke2
  datapath_provider        = "ADVANCED_DATAPATH" # Dataplane V2: NetworkPolicy is enforced

  release_channel {
    channel = var.release_channel
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  # Private nodes and no public IP endpoint. Operators reach the API through the DNS endpoint, which
  # Google authorizes with IAM per request (the GKE counterpart of IAP on gcp-rke2): no IP allowlist.
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = true
  }
  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = true
    }
  }
  master_authorized_networks_config {
    gcp_public_cidrs_access_enabled = false
  }

  # Pods get a federated identity from the cluster's public issuer (also what Tailscale WIF trusts),
  # never the node service account token.
  workload_identity_config {
    workload_pool = local.workload_pool
  }

  node_config {
    service_account = google_service_account.node.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }

  depends_on = [google_compute_router_nat.this, google_project_iam_member.node_default]
}

resource "google_container_node_pool" "default" {
  name       = "default"
  cluster    = google_container_cluster.this.id
  location   = var.zone
  node_count = var.node_count

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.machine_type
    disk_type       = "pd-balanced"
    disk_size_gb    = 100
    service_account = google_service_account.node.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"] # effective access is the IAM above
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }

  depends_on = [google_artifact_registry_repository_iam_member.node_read]
}
