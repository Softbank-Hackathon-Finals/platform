# Agent sandbox session VM

Docker host for the Agents SDK proposal sandbox (`bravo_agent.sandbox_provider`). It is a
separate VM so sandbox containers never share a kernel with the platform control plane.

- **Only client:** the subscription broker on the platform node. Docker listens on TLS 2376 with
  client-certificate verification (the authentication boundary); the security group admits only
  the platform node's address. No plaintext socket is exposed.
- **TLS material:** `operations/subscription/exec_pki.py OUT --docker-server <session IP>` issues
  a CA, the dockerd server pair and the broker client pair. Write `ca.pem`, `server-cert.pem`,
  `server-key.pem` to SSM SecureString under `tls_parameter_prefix`; the VM reads them at every
  docker start and does not start dockerd without them. Put `ca.pem`, `cert.pem`, `key.pem` in the
  broker's `DOCKER_CERT_PATH` and set broker settings `sandbox_docker` to
  `{"DOCKER_HOST": <output docker_host>, "DOCKER_TLS_VERIFY": "1", "DOCKER_CERT_PATH": <dir>}`.
- **Containers:** created by the sandbox with `network_mode="none"`; dockerd runs with
  `userns-remap`, `no-new-privileges` and `icc=false`; IMDS hop limit 1 keeps containers away from
  the instance role.

```sh
terraform init -backend-config=/private/agent-sandbox.backend.json
terraform plan -var-file=/private/session.auto.tfvars.json -out=/private/session.tfplan
```

No tfvars, backend values, plan/state or resource IDs belong in Git.
