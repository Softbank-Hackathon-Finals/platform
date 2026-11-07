# GCP foundation for a cluster in a reviewed project: the APIs a cluster stack needs and the network
# (network/gcp). Project creation and billing linkage stay manual (README "실제 생성 순서").
locals {
  labels = { project = "bravo-platform", component = var.component, "managed-by" = "terraform" }
  services = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "secretmanager.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "artifactregistry.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each           = local.services
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false # never switch an API off under other users of the project
}

module "network" {
  source       = "../../modules/network/gcp"
  project_id   = var.project_id
  region       = var.region
  name         = var.name
  labels       = local.labels
  node_cidr    = var.node_cidr
  pod_cidr     = var.pod_cidr
  service_cidr = var.service_cidr
  depends_on   = [google_project_service.required]
}

output "foundation" {
  description = "cluster-gke inputs (wired by infra/stacks/cluster-gke). Identifiers, not credentials."
  value       = merge(module.network.network, { services = sort(tolist(local.services)) })
}
