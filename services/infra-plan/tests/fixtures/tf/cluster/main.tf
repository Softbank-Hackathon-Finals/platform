# Test stand-in for infra/envs/source-eks: consumes the network output by wiring.
terraform {
  backend "local" {}
}

variable "vpc_id" { type = string }
variable "name" { type = string }

resource "terraform_data" "cluster" {
  input = { vpc_id = var.vpc_id, name = var.name }
}

output "operator" {
  sensitive = true
  value     = { cluster_name = var.name, vpc_id = var.vpc_id }
}
