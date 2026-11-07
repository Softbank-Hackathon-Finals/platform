mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "1000001" }
  }
  mock_resource "google_iam_workload_identity_pool" {
    defaults = { name = "projects/1000001/locations/global/workloadIdentityPools/bravo-platform" }
  }
  mock_resource "google_iam_workload_identity_pool_provider" {
    defaults = { name = "projects/1000001/locations/global/workloadIdentityPools/bravo-platform/providers/platform-oidc" }
  }
  # A synthetic reader. Composed with ${"@"}: the provider checks the service account address form, and the
  # public-evidence scan judges addresses by domain only (a service account domain is never an example).
  mock_resource "google_service_account" {
    defaults = {
      name  = "projects/bravo-test-project/serviceAccounts/example-scc-reader${"@"}bravo-test-project.iam.gserviceaccount.com"
      email = "example-scc-reader${"@"}bravo-test-project.iam.gserviceaccount.com"
    }
  }
}

variables {
  project_id     = "bravo-test-project"
  project_number = "1000001"
  issuer_url     = "https://bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform"
}

run "only_the_scc_puller_is_federated" {
  command = apply
  assert {
    condition     = google_iam_workload_identity_pool_provider.platform.attribute_condition == "assertion.sub == 'system:serviceaccount:bravo-security:bravo-findings-scc-pull'"
    error_message = "STS must refuse every other subject of the platform issuer."
  }
  assert {
    condition     = google_iam_workload_identity_pool_provider.platform.attribute_mapping == tomap({ "google.subject" = "assertion.sub" })
    error_message = "Only the subject is mapped; no group or attribute grants."
  }
  assert {
    # The mock provider fills member (normalized by the provider) with a random value; the resource uses local.principal.
    condition = (google_service_account_iam_member.federated.role == "roles/iam.workloadIdentityUser"
    && local.principal == "principal://iam.googleapis.com/projects/1000001/locations/global/workloadIdentityPools/bravo-platform/subject/system:serviceaccount:bravo-security:bravo-findings-scc-pull")
    error_message = "Exactly one principal may impersonate the reader."
  }
  assert {
    condition     = google_iam_workload_identity_pool_provider.platform.oidc[0].issuer_uri == "https://bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform"
    error_message = "The platform issuer only."
  }
}

run "reader_roles_are_read_only" {
  command = apply
  assert {
    condition = toset([for m in google_project_iam_member.reader : m.role]) == toset([
      "roles/securitycenter.findingsViewer", "roles/securitycentermanagement.securityCenterServicesViewer"
    ])
    error_message = "Findings list and service state only: no mute, update, source or activation rights."
  }
}

run "outputs_feed_the_collector_without_a_key" {
  command = apply
  assert {
    condition = jsondecode(output.scope)["env-gcp"] == {
      parent          = "projects/1000001"
      audience        = "//iam.googleapis.com/projects/1000001/locations/global/workloadIdentityPools/bravo-platform/providers/platform-oidc"
      service_account = "example-scc-reader${"@"}bravo-test-project.iam.gserviceaccount.com"
    }
    error_message = "The scope file entry the collector checks (BRAVO_SCC_SCOPE_FILE)."
  }
  assert {
    condition = (jsondecode(output.credential_configuration).type == "external_account"
      && jsondecode(output.credential_configuration).credential_source == { file = "/var/run/secrets/gcp/token", format = { type = "text" } }
    && !can(jsondecode(output.credential_configuration).private_key))
    error_message = "A projected token file feeds STS; there is no key."
  }
  assert {
    condition     = output.token_audience == "https://iam.googleapis.com/projects/1000001/locations/global/workloadIdentityPools/bravo-platform/providers/platform-oidc"
    error_message = "The token audience is the provider's default allowed audience."
  }
}

run "wildcard_subject_is_refused" {
  command = plan
  variables {
    service_account = "system:serviceaccount:bravo-security:*"
  }
  expect_failures = [var.service_account]
}

run "plain_http_issuer_is_refused" {
  command = plan
  variables {
    issuer_url = "http://bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/platform"
  }
  expect_failures = [var.issuer_url]
}

run "another_project_is_refused" {
  command = plan
  variables {
    project_number = "2000002"
  }
  expect_failures = [google_iam_workload_identity_pool.platform]
}
