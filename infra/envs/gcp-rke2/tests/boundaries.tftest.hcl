mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "1000001" }
  }
}

variables {
  project_id     = "bravo-test-project"
  project_number = "1000001"
}

run "private_node_iap_only" {
  command = apply
  assert {
    condition     = length(google_compute_instance.node.network_interface[0].access_config) == 0
    error_message = "The node must not have an external address."
  }
  assert {
    condition     = google_compute_firewall.iap.source_ranges == toset(["35.235.240.0/20"]) && google_compute_firewall.iap.direction == "INGRESS"
    error_message = "Ingress is allowed from the IAP range only."
  }
  assert {
    condition     = length(google_compute_firewall.iap.allow) == 1 && one(google_compute_firewall.iap.allow).ports == tolist(["22", "6443"])
    error_message = "Only SSH and the API server are reachable through IAP."
  }
  assert {
    condition     = google_compute_instance.node.metadata["enable-oslogin"] == "TRUE" && google_compute_instance.node.metadata["block-project-ssh-keys"] == "TRUE"
    error_message = "Access goes through OS Login; project-wide SSH keys are blocked."
  }
  assert {
    condition     = google_artifact_registry_repository_iam_member.node_read.role == "roles/artifactregistry.reader"
    error_message = "The node may only read its own image repository."
  }
  assert {
    condition     = strcontains(google_compute_instance.node.metadata["user-data"], "profile: cis") && strcontains(google_compute_instance.node.metadata["user-data"], "matchImages: [\"asia-northeast3-docker.pkg.dev\"]")
    error_message = "User-data must carry the shared CIS bootstrap and the registry credential provider."
  }
  assert {
    condition     = strcontains(google_compute_instance.node.metadata["user-data"], "name: bravo-deny-instance-metadata") && strcontains(google_compute_instance.node.metadata["user-data"], "defaultAction: Pass")
    error_message = "User-data must apply the pod metadata guard with the kube-admin Pass tier."
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
    name = "bravo-gke-node"
  }
  expect_failures = [var.name]
}
