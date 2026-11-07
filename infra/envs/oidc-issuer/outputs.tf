output "issuers" {
  description = "Issuer URL per environment: the cluster's service-account-issuer and the federated identity's issuer."
  value       = local.issuer
}
