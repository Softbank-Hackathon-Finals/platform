locals {
  sides = {
    source = { tunnel = var.tunnels.source, host = var.hostnames.source, app = "http://demo-chat.source-dev.svc.cluster.local:8080" }
    target = { tunnel = var.tunnels.target, host = var.hostnames.target, app = "http://demo-chat.target-dev.svc.cluster.local:8080" }
  }
  # Keycloak lives only in the destination, so its address goes straight to the target tunnel, never through
  # the weighted LB: the issuer URL must not move while traffic shifts (target-workloads identity.py).
  # Only the bravo realm and the theme files go out. /admin/ and the master realm share port 8080 and fall
  # through to 404; administration stays behind kubectl port-forward. The whole sign-in flow uses only these
  # two prefixes (Keycloak 26.0.7 container walk, README).
  login_paths = "^/(realms/bravo|resources)/"
  login       = var.hostnames.login == null ? [] : [{ hostname = var.hostnames.login, path = local.login_paths, service = "http://target-login.target-dev.svc.cluster.local:8080" }]
  records     = merge({ for k, s in local.sides : k => { host = s.host, tunnel = s.tunnel } }, var.hostnames.login == null ? {} : { login = { host = var.hostnames.login, tunnel = var.tunnels.target } })
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "edge" {
  for_each   = local.sides
  account_id = var.account_id
  tunnel_id  = each.value.tunnel
  config = {
    ingress = concat(
      [{ hostname = each.value.host, service = each.value.app }],
      each.key == "target" ? local.login : [],
      [{ service = "http_status:404" }],
    )
  }
}

resource "cloudflare_dns_record" "tunnel" {
  for_each = local.records
  zone_id  = var.zone_id
  name     = each.value.host
  type     = "CNAME"
  content  = "${each.value.tunnel}.cfargotunnel.com"
  proxied  = true
  ttl      = 1
  comment  = "Team Bravo T17 existing tunnel origin"
}

resource "cloudflare_load_balancer_monitor" "ready" {
  account_id       = var.account_id
  description      = "bravo-edge-ready"
  type             = "https"
  method           = "GET"
  path             = "/readyz"
  port             = 443
  expected_codes   = "200"
  interval         = 60
  timeout          = 5
  retries          = 2
  allow_insecure   = false
  follow_redirects = false
}

resource "cloudflare_load_balancer_pool" "edge" {
  for_each        = local.sides
  account_id      = var.account_id
  name            = "bravo-${each.key}-pool"
  description     = "Team Bravo T17 ${each.key} origin for ${var.hostnames.app}"
  enabled         = true
  minimum_origins = 1
  monitor         = cloudflare_load_balancer_monitor.ready.id
  origins = [{
    name    = "bravo-${each.key}"
    address = "${each.value.tunnel}.cfargotunnel.com"
    enabled = true
    weight  = 1
    header  = { host = [each.value.host] }
  }]
}

resource "cloudflare_load_balancer" "edge" {
  zone_id          = var.zone_id
  name             = var.hostnames.app
  description      = "Team Bravo T17 joint measurement; enable only after both application origins are verified"
  enabled          = false # a new LB starts off; the run owner enables it after both origins pass (edge-control never does)
  proxied          = true
  steering_policy  = "random"
  session_affinity = "none"
  default_pools    = [cloudflare_load_balancer_pool.edge["source"].id, cloudflare_load_balancer_pool.edge["target"].id]
  fallback_pool    = cloudflare_load_balancer_pool.edge["source"].id
  adaptive_routing = { failover_across_pools = false }
  # A new LB starts all-source. After that the weights belong to edge-control (one writer, its own journal).
  random_steering = {
    default_weight = 0
    pool_weights   = { (cloudflare_load_balancer_pool.edge["source"].id) = 1, (cloudflare_load_balancer_pool.edge["target"].id) = 0 }
  }
  lifecycle {
    ignore_changes = [random_steering, enabled]
  }
}

import {
  for_each = var.adopt.configs ? local.sides : {}
  to       = cloudflare_zero_trust_tunnel_cloudflared_config.edge[each.key]
  id       = "${var.account_id}/${each.value.tunnel}"
}

import {
  for_each = var.adopt.dns
  to       = cloudflare_dns_record.tunnel[each.key]
  id       = "${var.zone_id}/${each.value}"
}

import {
  for_each = var.adopt.pools
  to       = cloudflare_load_balancer_pool.edge[each.key]
  id       = "${var.account_id}/${each.value}"
}

import {
  for_each = var.adopt.monitor == null ? {} : { ready = var.adopt.monitor }
  to       = cloudflare_load_balancer_monitor.ready
  id       = "${var.account_id}/${each.value}"
}

import {
  for_each = var.adopt.lb == null ? {} : { edge = var.adopt.lb }
  to       = cloudflare_load_balancer.edge
  id       = "${var.zone_id}/${each.value}"
}
