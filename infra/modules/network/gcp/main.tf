# One custom-mode VPC, one regional subnet with secondary ranges for GKE (VPC-native), and Cloud NAT so
# private nodes reach registries, Cloudflare and the tailnet without public node IPs. A custom VPC denies
# ingress by default; this module opens nothing.
locals {
  pod_range     = "${var.name}-pods"
  service_range = "${var.name}-services"
}

resource "google_compute_network" "cluster" {
  project                 = var.project_id
  name                    = var.name
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "cluster" {
  project                  = var.project_id
  name                     = "${var.name}-${var.region}"
  region                   = var.region
  network                  = google_compute_network.cluster.id
  ip_cidr_range            = var.node_cidr
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = local.pod_range
    ip_cidr_range = var.pod_cidr
  }
  secondary_ip_range {
    range_name    = local.service_range
    ip_cidr_range = var.service_cidr
  }

  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_router" "egress" {
  project = var.project_id
  name    = "${var.name}-egress"
  region  = var.region
  network = google_compute_network.cluster.id
}

resource "google_compute_router_nat" "egress" {
  project                            = var.project_id
  name                               = "${var.name}-nat"
  router                             = google_compute_router.egress.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.cluster.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}
