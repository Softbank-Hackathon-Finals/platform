mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "1000001" }
  }
}

variables {
  project_id     = "bravo-test-project"
  project_number = "1000001"
}

run "eso_reads_prefixed_secrets_only" {
  command = apply
  assert {
    condition     = google_project_iam_member.eso_secret_reader.member == "principal://iam.googleapis.com/projects/1000001/locations/global/workloadIdentityPools/bravo-test-project.svc.id.goog/subject/ns/external-secrets/sa/external-secrets"
    error_message = "Only the ESO controller's Kubernetes service account is granted, through Workload Identity."
  }
  assert {
    condition     = google_project_iam_member.eso_secret_reader.role == "roles/secretmanager.secretAccessor" && google_project_iam_member.eso_secret_reader.condition[0].expression == "resource.name.startsWith(\"projects/1000001/secrets/bravo-\")"
    error_message = "The grant is read-only and limited to the secret prefix."
  }
  assert {
    condition     = !google_project_service.secret_manager.disable_on_destroy
    error_message = "The API is never switched off under other users of the project."
  }
}

run "other_project_is_refused" {
  command = plan
  variables {
    project_number = "1000002"
  }
  expect_failures = [google_project_service.secret_manager]
}

run "empty_prefix_is_refused" {
  command = plan
  variables {
    secret_prefix = ""
  }
  expect_failures = [var.secret_prefix]
}
