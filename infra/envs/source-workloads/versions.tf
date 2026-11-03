terraform {
  required_version = ">= 1.16.5, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.71.0"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = { project = "bravo-platform", component = "source", managed-by = "terraform" }
  }
}
