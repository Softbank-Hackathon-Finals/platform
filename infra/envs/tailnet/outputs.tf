output "operator_identities" {
  description = "Helm values for each environment's operator: oauth.clientId and oauth.audience (not secrets)."
  value = {
    for env, identity in tailscale_federated_identity.operator : env => {
      client_id = identity.id
      audience  = identity.audience
    }
  }
}
