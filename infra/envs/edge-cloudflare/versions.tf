terraform {
  required_version = ">= 1.16.5, < 2.0.0"
  backend "s3" {}
  required_providers {
    cloudflare = { source = "cloudflare/cloudflare", version = "~> 5.28.1" }
  }
}

# API token comes from CLOUDFLARE_API_TOKEN at plan/apply time. It never enters tfvars or state.
provider "cloudflare" {}
