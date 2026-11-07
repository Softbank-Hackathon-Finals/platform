output "instance" {
  value = google_compute_instance.node.name
}

output "zone" {
  value = google_compute_instance.node.zone
}

output "project_id" {
  value = var.project_id
}

output "internal_ip" {
  value = google_compute_instance.node.network_interface[0].network_ip
}

output "registry" {
  value = "${local.registry}/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "boot_image" {
  value = google_compute_instance.node.boot_disk[0].initialize_params[0].image
}
