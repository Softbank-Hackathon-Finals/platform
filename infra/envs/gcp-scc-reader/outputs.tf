# Numbers and identities stay out of Git: write these outputs into the private ConfigMap/Secret files.

output "token_audience" {
  description = "Audience of the collector's projected ServiceAccount token (the provider's default allowed audience)."
  value       = "https://iam.googleapis.com/${google_iam_workload_identity_pool_provider.platform.name}"
}

output "scope" {
  description = "BRAVO_SCC_SCOPE_FILE entry: {environment_id: {parent, audience, service_account}}."
  value = jsonencode({ (var.environment_id) = {
    parent          = "projects/${data.google_project.this.number}"
    audience        = "//iam.googleapis.com/${google_iam_workload_identity_pool_provider.platform.name}"
    service_account = google_service_account.reader.email
  } })
}

output "credential_configuration" {
  description = "GOOGLE_APPLICATION_CREDENTIALS: external_account fed by the projected token file, not a key."
  value = jsonencode({
    universe_domain                   = "googleapis.com"
    type                              = "external_account"
    audience                          = "//iam.googleapis.com/${google_iam_workload_identity_pool_provider.platform.name}"
    subject_token_type                = "urn:ietf:params:oauth:token-type:jwt"
    token_url                         = "https://sts.googleapis.com/v1/token"
    credential_source                 = { file = var.token_path, format = { type = "text" } }
    service_account_impersonation_url = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${google_service_account.reader.email}:generateAccessToken"
  })
}
