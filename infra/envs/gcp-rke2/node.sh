#!/usr/bin/env bash
# Operator access to the GCP RKE2 node, through IAP and OS Login only (no public address, no stored key).
# Same verbs as infra/envs/onprem/local-vm.sh so the T45 host-action adapter can swap one for the other.
#
#   BRAVO_GCP_NODE=<private>/node.json  (terraform output -json)
#   ./node.sh wait [timeout_s]          cloud-init finished and RKE2 Ready
#   ./node.sh ssh <command...>          run on the node; stdin is passed through
#   ./node.sh params                    T45 target stack params + cluster UID as JSON
#   ./node.sh kubeconfig <out> <port>   admin kubeconfig rewritten to the local IAP tunnel port (0600)
#   ./node.sh tunnel <port>             foreground IAP tunnel to the API server
#   ./node.sh import-image <image|tar>  containerd import of a docker image or an OCI/docker archive (amd64)
set -euo pipefail

: "${BRAVO_GCP_NODE:?set BRAVO_GCP_NODE to the terraform output JSON}"
read -r PROJECT ZONE INSTANCE < <(python3 -I -c '
import json, sys
o = json.load(open(sys.argv[1]))
print(o["project_id"]["value"], o["zone"]["value"].rsplit("/", 1)[-1], o["instance"]["value"])' "$BRAVO_GCP_NODE")
WHERE=(--project "$PROJECT" --zone "$ZONE")
KUBECTL="sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml"

node_ssh() {
  # OS Login key expires on its own; IAP is the transport. -q keeps the remote output clean.
  gcloud compute ssh "$INSTANCE" "${WHERE[@]}" --tunnel-through-iap --ssh-key-expire-after=8h --quiet \
    -- -q -o ConnectTimeout=20 -o ServerAliveInterval=15 "$@"
}

case "${1:-}" in
  ssh)
    shift
    node_ssh "$@"
    ;;
  wait)
    timeout=${2:-900}
    start=$SECONDS
    until node_ssh test -f /var/lib/bravo-runtime-ready </dev/null 2>/dev/null; do
      (( SECONDS - start > timeout )) && { echo "not ready after ${timeout}s" >&2; exit 1; }
      sleep 10
    done
    node_ssh "$KUBECTL wait node --all --for=condition=Ready --timeout=120s" </dev/null >/dev/null
    echo "ready after $((SECONDS - start))s"
    ;;
  params)
    node_ssh "$KUBECTL get ns kube-system -o jsonpath='{.metadata.uid}'; echo;
      $KUBECTL get nodes -o jsonpath='{.items[0].metadata.name} {.items[0].status.nodeInfo.architecture} {.items[0].status.nodeInfo.kubeletVersion}'; echo;
      $KUBECTL get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}'; echo;
      $KUBECTL get endpoints kubernetes -n default -o jsonpath='{.subsets[0].addresses[0].ip}'" </dev/null |
      python3 -I -c '
import json, sys
uid, node, service_ip, endpoint_ip = sys.stdin.read().split("\n")[:4]
name, arch, version = node.split()
print(json.dumps({"node_name": name, "arch": arch, "api_service_ip": service_ip,
                  "api_endpoint_ip": endpoint_ip, "kube_version": version.split("+", 1)[0],
                  "cluster_uid": uid}))'
    ;;
  kubeconfig)
    out=${2:?output path}; port=${3:?local tunnel port}
    (umask 077; node_ssh sudo cat /etc/rancher/rke2/rke2.yaml </dev/null |
      sed "s#https://127.0.0.1:6443#https://127.0.0.1:${port}#" >"$out")
    ;;
  tunnel)
    port=${2:?local port}
    exec gcloud compute start-iap-tunnel "$INSTANCE" 6443 --local-host-port="127.0.0.1:${port}" "${WHERE[@]}"
    ;;
  import-image)
    image=${2:?image or archive}
    # An archive is imported byte for byte (the scanned OCI layout); an image name goes through docker save.
    { if [[ -f "$image" ]]; then cat "$image"; else docker save "$image"; fi; } | node_ssh \
      sudo /var/lib/rancher/rke2/bin/ctr --address /run/k3s/containerd/containerd.sock -n k8s.io images import -
    ;;
  *)
    sed -n '2,12p' "$0" >&2
    exit 2
    ;;
esac
