terraform {
  required_version = ">= 1.16.5, < 2.0.0"
  required_providers {
    tailscale = {
      source  = "tailscale/tailscale"
      version = "~> 0.29.3"
    }
  }
  backend "s3" {}
}

# Credentials come only from the runtime: TAILSCALE_OAUTH_CLIENT_ID (a federated identity) and an
# OIDC token (TAILSCALE_IDENTITY_TOKEN or the provider's runtime discovery). No secret is stored.
provider "tailscale" {
  tailnet = var.tailnet
}
