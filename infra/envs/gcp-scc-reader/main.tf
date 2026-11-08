locals {
  subject = var.service_account
  # The one principal allowed to impersonate the reader.
  principal = "principal://iam.googleapis.com/${google_iam_workload_identity_pool.platform.name}/subject/${local.subject}"
}

data "google_project" "this" {}

# APIs the collector calls. They stay on at destroy: other stacks may use them.
resource "google_project_service" "this" {
  for_each = toset([
    "iam.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com",
    "securitycenter.googleapis.com", "securitycentermanagement.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# The platform cluster's ServiceAccount tokens. Only the SCC collector's subject is mapped at all;
# every other subject of the same issuer is refused by STS before any IAM check.
resource "google_iam_workload_identity_pool" "platform" {
  workload_identity_pool_id = var.pool_id
  display_name              = "bravo platform"
  description               = "T33: platform cluster ServiceAccount tokens (public OIDC issuer)"
  depends_on                = [google_project_service.this]
  lifecycle {
    precondition {
      condition     = data.google_project.this.number == var.project_number
      error_message = "The provider project does not match the reviewed project_number."
    }
  }
}

resource "google_iam_workload_identity_pool_provider" "platform" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.platform.workload_identity_pool_id
  workload_identity_pool_provider_id = var.provider_id
  display_name                       = "platform issuer"
  attribute_mapping                  = { "google.subject" = "assertion.sub" }
  attribute_condition                = "assertion.sub == '${local.subject}'"
  oidc {
    issuer_uri        = var.issuer_url
    allowed_audiences = [var.token_audience]
  }
}

# Read-only reader the collector impersonates. No key is ever created for it.
resource "google_service_account" "reader" {
  account_id   = var.reader_id
  display_name = "bravo SCC reader"
  description  = "T33: Security Command Center findings and service state, read only"
}

resource "google_service_account_iam_member" "federated" {
  service_account_id = google_service_account.reader.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.principal
}

# findings.list for the collector, securityCenterServices.get to tell "0 findings" from "detector off".
resource "google_project_iam_member" "reader" {
  for_each = toset([
    "roles/securitycenter.findingsViewer",
    "roles/securitycentermanagement.securityCenterServicesViewer",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.reader.email}"
}
