# Test stand-in for infra/envs/aws-foundation: builtin provider only, no network.
terraform {
  backend "local" {}
}

variable "cidr" { type = string }

resource "terraform_data" "vpc" {
  input            = var.cidr
  triggers_replace = [var.cidr] # a CIDR change replaces the VPC, as in AWS
}

output "foundation" {
  sensitive = true
  value     = { vpcs = { source = { id = "vpc-${substr(sha256(terraform_data.vpc.output), 0, 8)}", cidr = var.cidr } } }
}
