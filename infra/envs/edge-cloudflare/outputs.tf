output "login_issuer" {
  description = "AUTH_ISSUER for the destination demo-chat once Keycloak is up (null while login is closed)."
  value       = var.hostnames.login == null ? null : "https://${var.hostnames.login}/realms/bravo"
}

output "lb_binding" {
  description = "What edge-control registers: LB and the two pools. Weights are not an output; edge-control reads them live."
  value = {
    lb_id       = cloudflare_load_balancer.edge.id
    source_pool = cloudflare_load_balancer_pool.edge["source"].id
    target_pool = cloudflare_load_balancer_pool.edge["target"].id
  }
}
