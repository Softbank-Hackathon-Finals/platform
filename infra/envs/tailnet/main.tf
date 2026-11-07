# T48 / ADR-0035: the tailnet policy and each environment's operator identity, from Git only.

resource "tailscale_acl" "this" {
  acl = file(var.policy_file)
  # Never take over a live policy by accident: migrate it into infra/tailnet first, then
  # `terraform import tailscale_acl.this acl`. A fresh apply without the import fails.
  overwrite_existing_content = false
  reset_acl_on_destroy       = false
}

# One trust per environment: that cluster's operator ServiceAccount token, nothing else.
# Deleting the entry from identities.json is the disengage: the operator can no longer authenticate.
resource "tailscale_federated_identity" "operator" {
  for_each = var.identities

  description = "bravo ${each.key} kubernetes operator (T48)"
  issuer      = each.value.issuer
  subject     = "system:serviceaccount:${each.value.namespace}:${each.value.service_account}"
  tags        = ["tag:bravo-${each.key}-operator"]
  scopes      = ["devices:core", "auth_keys", "services"]

  depends_on = [tailscale_acl.this] # the tag owner must exist first
}
