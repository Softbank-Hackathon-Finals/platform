# Test stand-in for infra/envs/cluster-gke / cluster-eks: a "cluster" whose endpoint is an output.
terraform {
  backend "local" {}
}

variable "endpoint" { type = string }

resource "terraform_data" "cluster" {
  input = { endpoint = var.endpoint }
}

output "cluster" {
  value = { name = "bravo-test", endpoint = terraform_data.cluster.output.endpoint }
}
