# T48: each run makes only its own tailnet's operator identities, with the exact ServiceAccount subject.
mock_provider "tailscale" {}

variables {
  identities = {
    t49   = { issuer = "https://issuer.example/t49" }
    other = { issuer = "https://issuer.example/other", tailnet = "workload" }
  }
}

run "default_tailnet_only" {
  command = plan

  assert {
    condition     = keys(tailscale_federated_identity.operator) == ["t49"]
    error_message = "a workload-tailnet identity was planned in the default tailnet"
  }

  assert {
    condition     = tailscale_federated_identity.operator["t49"].subject == "system:serviceaccount:tailscale-system:operator"
    error_message = "the subject must be the operator ServiceAccount, nothing wider"
  }

  assert {
    condition     = tailscale_federated_identity.operator["t49"].tags == toset(["tag:bravo-t49-operator"])
    error_message = "the identity must carry only its environment's operator tag"
  }
}

run "workload_tailnet_only" {
  command = plan

  variables {
    tailnet_key = "workload"
  }

  assert {
    condition     = keys(tailscale_federated_identity.operator) == ["other"]
    error_message = "a default-tailnet identity was planned in the workload tailnet"
  }
}
