module "network" {
  source             = "../../modules/network/aws"
  account_id         = var.account_id
  region             = var.region
  name               = var.name
  availability_zones = var.availability_zones
  network_cidr       = var.network_cidr
}

output "foundation" {
  description = "T6/T7 integration inputs; resource identifiers, not credentials."
  value       = module.network.foundation
}
