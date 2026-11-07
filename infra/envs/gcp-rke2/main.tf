locals {
  # Google's published IAP TCP forwarding range: the only ingress source.
  iap_range = "35.235.240.0/20"
  registry  = "${var.region}-docker.pkg.dev"
  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    rke2_version   = var.rke2_version
    rke2_bootstrap = file("${path.module}/../../shared/rke2/bootstrap.sh")
    audit_policy   = file("${path.module}/../../../services/control-runner/templates/rke2-audit-policy.yaml")
    metadata_guard = file("${path.module}/metadata-guard.yaml")
    registry       = local.registry
  })
}

# GCE updates metadata in place and cloud-init runs once, so a user-data change must replace the node
# (the AWS env uses user_data_replace_on_change for the same reason).
resource "terraform_data" "user_data" {
  input = sha256(local.user_data)
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
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

# Egress only (RKE2 release, public images). No inbound path is created.
resource "google_compute_router" "this" {
  name    = var.name
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  name                               = var.name
  router                             = google_compute_router.this.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"
  subnetwork {
    name                    = google_compute_subnetwork.this.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }
}

# A custom VPC denies ingress by default. Operators reach SSH and the API through IAP only,
# the GCP counterpart of the AWS SSM session (IAM-checked, no public address, no key in Git).
resource "google_compute_firewall" "iap" {
  name          = "${var.name}-iap"
  network       = google_compute_network.this.id
  direction     = "INGRESS"
  source_ranges = [local.iap_range]
  target_tags   = [var.name]
  allow {
    protocol = "tcp"
    ports    = ["22", "6443"]
  }
}

resource "google_service_account" "node" {
  account_id   = "${var.name}-node"
  display_name = "${var.name} RKE2 node"
}

# Candidate images for this destination. The node reads this repository only.
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

resource "google_compute_instance" "node" {
  name         = var.name
  machine_type = var.machine_type
  tags         = [var.name]

  boot_disk {
    initialize_params {
      image = var.image
      size  = var.disk_gb
      type  = "pd-balanced"
    }
  }

  # No access_config block: the instance has no external address.
  network_interface {
    subnetwork = google_compute_subnetwork.this.id
  }

  service_account {
    email  = google_service_account.node.email
    scopes = ["cloud-platform"] # effective access is the IAM above: one repository, read only
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  metadata = {
    enable-oslogin         = "TRUE"
    block-project-ssh-keys = "TRUE"
    user-data              = local.user_data
  }

  lifecycle {
    replace_triggered_by = [terraform_data.user_data]
  }
  depends_on = [google_compute_router_nat.this, google_artifact_registry_repository_iam_member.node_read]
}
