#!/usr/bin/env bash
# Common T2/T7 bootstrap candidate. No AWS credentials or environment addresses.
set -euo pipefail
umask 077
: "${RKE2_VERSION:?an exact RKE2 release is required}"
[[ "$RKE2_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+\+rke2r[0-9]+$ ]]
if systemctl is-active --quiet rke2-server; then
  echo 'Existing RKE2 is running; refusing to replace its configuration.' >&2
  exit 1
fi
getent group etcd >/dev/null || groupadd --system etcd
id etcd >/dev/null 2>&1 || useradd --system --gid etcd --shell /usr/sbin/nologin --no-create-home etcd
swapoff -a
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
mkdir -p /etc/rancher/rke2 /var/lib/rancher/rke2/server/manifests
# Fetch installer from the same immutable release tag as the binary.
curl --fail --silent --show-error --location --retry 3 \
  "https://raw.githubusercontent.com/rancher/rke2/${RKE2_VERSION}/install.sh" -o /run/rke2-install.sh
INSTALL_RKE2_VERSION="$RKE2_VERSION" INSTALL_RKE2_METHOD=tar sh /run/rke2-install.sh
cp /usr/local/share/rke2/rke2-cis-sysctl.conf /etc/sysctl.d/60-rke2-cis.conf
systemctl restart systemd-sysctl
cat > /etc/rancher/rke2/config.yaml <<'YAML'
profile: cis
write-kubeconfig-mode: "0600"
disable:
  - rke2-ingress-nginx
  - rke2-traefik
YAML
systemctl enable --now rke2-server
export KUBECONFIG=/etc/rancher/rke2/rke2.yaml
kubectl=/var/lib/rancher/rke2/bin/kubectl
for attempt in $(seq 1 120); do
  if "$kubectl" get --raw=/readyz >/dev/null 2>&1; then break; fi
  sleep 5
done
"$kubectl" wait node --all --for=condition=Ready --timeout=300s
