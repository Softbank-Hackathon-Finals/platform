mock_provider "cloudflare" {}

override_resource {
  target = cloudflare_load_balancer_pool.edge["source"]
  values = { id = "pool-source" }
}

override_resource {
  target = cloudflare_load_balancer_pool.edge["target"]
  values = { id = "pool-target" }
}

variables {
  account_id = "account"
  zone_id    = "zone"
  tunnels    = { source = "tunnel-source", target = "tunnel-target" }
  hostnames  = { app = "chat.example.com", source = "source.example.com", target = "target.example.com" }
}

run "login_stays_closed_until_declared" {
  command = apply
  assert {
    condition     = length(cloudflare_zero_trust_tunnel_cloudflared_config.edge["target"].config.ingress) == 2 && !contains(keys(cloudflare_dns_record.tunnel), "login")
    error_message = "Without a login hostname the target tunnel serves only demo-chat and no login record exists."
  }
  assert {
    condition     = output.login_issuer == null
    error_message = "No issuer is published while login is closed."
  }
}

run "login_goes_only_to_target_keycloak" {
  command = apply
  variables {
    hostnames = { app = "chat.example.com", source = "source.example.com", target = "target.example.com", login = "login.example.com" }
  }
  assert {
    condition = jsonencode([for r in cloudflare_zero_trust_tunnel_cloudflared_config.edge["target"].config.ingress : [r.hostname, r.service]]) == jsonencode([
      ["target.example.com", "http://demo-chat.target-dev.svc.cluster.local:8080"],
      ["login.example.com", "http://target-identity.target-dev.svc.cluster.local:8080"],
      [null, "http_status:404"],
    ])
    error_message = "Target tunnel: demo-chat, then Keycloak on the login host, then 404 for everything else."
  }
  assert {
    condition     = !contains([for r in cloudflare_zero_trust_tunnel_cloudflared_config.edge["source"].config.ingress : r.hostname], "login.example.com")
    error_message = "The source tunnel never serves the login host."
  }
  assert {
    condition     = cloudflare_dns_record.tunnel["login"].content == "tunnel-target.cfargotunnel.com" && cloudflare_dns_record.tunnel["login"].proxied
    error_message = "The login host points straight at the target tunnel through Cloudflare, not at the weighted LB."
  }
  assert {
    condition     = output.login_issuer == "https://login.example.com/realms/bravo"
    error_message = "The issuer matches identity.py's realm."
  }
}

run "source_tunnel_ends_in_404" {
  command = apply
  assert {
    condition = jsonencode([for r in cloudflare_zero_trust_tunnel_cloudflared_config.edge["source"].config.ingress : [r.hostname, r.service]]) == jsonencode([
      ["source.example.com", "http://demo-chat.source-dev.svc.cluster.local:8080"],
      [null, "http_status:404"],
    ])
    error_message = "Source tunnel: demo-chat on its own host, 404 for everything else."
  }
}

# edge-control refuses an LB that does not look like this (services/edge-control README "자원 조건과 기록").
run "lb_matches_what_edge_control_accepts" {
  command = apply
  assert {
    condition     = cloudflare_load_balancer.edge.steering_policy == "random" && cloudflare_load_balancer.edge.session_affinity == "none" && cloudflare_load_balancer.edge.proxied
    error_message = "Random steering, no session affinity, proxied."
  }
  assert {
    condition     = cloudflare_load_balancer.edge.fallback_pool == "pool-source" && jsonencode(cloudflare_load_balancer.edge.default_pools) == jsonencode(["pool-source", "pool-target"])
    error_message = "Exactly the two pools, source first and as fallback."
  }
  assert {
    condition     = cloudflare_load_balancer.edge.adaptive_routing.failover_across_pools == false
    error_message = "No retries across pools: that would move requests outside the declared weights."
  }
  assert {
    condition     = cloudflare_load_balancer.edge.random_steering.pool_weights == tomap({ "pool-source" = 1, "pool-target" = 0 }) && cloudflare_load_balancer.edge.random_steering.default_weight == 0
    error_message = "A new LB starts with all traffic on the source."
  }
  assert {
    condition     = alltrue([for p in cloudflare_load_balancer_pool.edge : length(p.origins) == 1 && p.monitor == cloudflare_load_balancer_monitor.ready.id])
    error_message = "One tunnel origin per pool, health-checked by the shared /readyz monitor."
  }
}

run "rejects_login_on_the_weighted_address" {
  command = plan
  variables {
    hostnames = { app = "chat.example.com", source = "source.example.com", target = "target.example.com", login = "chat.example.com" }
  }
  expect_failures = [var.hostnames]
}
