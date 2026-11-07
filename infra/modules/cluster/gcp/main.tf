# GKE Standard (not Autopilot): the Tailscale operator's proxies need NET_ADMIN, which Autopilot refuses, and
# CNPG runs as an ordinary StatefulSet-like operator on Standard nodes with the default `standard-rwo` disks.
# Private nodes egress through Cloud NAT (network/gcp). The control plane is reached through its DNS endpoint
# (IAM-authenticated); the public IP endpoint is off. Workload Identity is on for every node pool.
locals {
  workload_pool = "${var.project_id}.svc.id.goog"
}

resource "google_service_account" "node" {
  project      = var.project_id
  account_id   = "${substr(var.name, 0, 24)}-node"
  display_name = "GKE nodes of ${var.name} (logs, metrics, image pulls only)"
}

resource "google_project_iam_member" "node" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
    "roles/artifactregistry.reader",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.node.email}"
}

resource "google_container_cluster" "this" {
  project             = var.project_id
  name                = var.name
  location            = var.location
  network             = var.network
  subnetwork          = var.subnetwork
  resource_labels     = var.labels
  deletion_protection = false # demo cluster: `terraform destroy` after the event

  # A separately managed node pool replaces the default one.
  remove_default_node_pool = true
  initial_node_count       = 1

  release_channel {
    channel = var.release_channel
  }

  networking_mode   = "VPC_NATIVE"
  datapath_provider = "ADVANCED_DATAPATH" # Dataplane V2 enforces NetworkPolicy
  ip_allocation_policy {
    cluster_secondary_range_name  = var.pod_range_name
    services_secondary_range_name = var.service_range_name
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = var.master_cidr
  }

  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = true
    }
    ip_endpoints_config {
      enabled = false
    }
  }

  # IP endpoint access (off above) stays limited to these private ranges; the DNS endpoint uses IAM.
  master_authorized_networks_config {
    gcp_public_cidrs_access_enabled = false
    dynamic "cidr_blocks" {
      for_each = var.authorized_networks
      content {
        cidr_block   = cidr_blocks.value
        display_name = "authorized-${cidr_blocks.key}"
      }
    }
  }

  workload_identity_config {
    workload_pool = local.workload_pool
  }

  enable_shielded_nodes = true

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "APISERVER", "WORKLOADS"]
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }
}

resource "google_container_node_pool" "base" {
  project    = var.project_id
  name       = "base"
  location   = var.location
  cluster    = google_container_cluster.this.name
  node_count = var.node_count

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.machine_type
    disk_size_gb    = var.disk_size_gb
    disk_type       = "pd-balanced"
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.node.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    resource_labels = var.labels
    labels          = { "bravo.io/pool" = "base" }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }

  depends_on = [google_project_iam_member.node]
}
