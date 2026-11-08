# T48 / ADR-0035: each environment's operator identity, from Git only. The tailnet policy is not
# written here: the tailnet-policy workflow (#667) is its only writer.

# One trust per environment: that cluster's operator ServiceAccount token, nothing else.
# Deleting the entry from identities.json is the disengage: the operator can no longer authenticate.
# The tag owner (tag:bravo-<env>-operator) must already be in the policy, from the tailnet-policy fragment.
resource "tailscale_federated_identity" "operator" {
  # Only this tailnet's environments: a workload-tailnet identity must not be made in the default tailnet.
  for_each = { for env, identity in var.identities : env => identity if identity.tailnet == var.tailnet_key }

  description = "bravo ${each.key} kubernetes operator (T48)"
  issuer      = each.value.issuer
  subject     = "system:serviceaccount:${each.value.namespace}:${each.value.service_account}"
  tags        = ["tag:bravo-${each.key}-operator"]
  scopes      = ["devices:core", "auth_keys", "services"]
}
