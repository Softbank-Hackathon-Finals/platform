#!/usr/bin/env bash
# 새 VM에 넣을 cloud-init user-data를 표준 출력으로 낸다. Terraform이 없는 온프렘에서 T7의 templatefile과 같은 일을 한다.
# RKE2 · CIS 기준은 infra/shared/rke2/bootstrap.sh 한 곳에 두고, 여기에는 온프렘에만 필요한 Tailscale 설치를 더한다.
# 비밀 값을 넣지 않는다. user-data는 VM 안에 그대로 남는다. Tailscale 로그인은 T30에서 한다.
set -euo pipefail

# 출발지 EKS(T6)와 플랫폼 RKE2(T7)의 Kubernetes 1.35에 맞춘다.
RKE2_VERSION=v1.35.9+rke2r1
HERE=$(cd "$(dirname "$0")" && pwd)

cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
echo 'RKE2_VERSION=$RKE2_VERSION' >/etc/bravo-node.env
export RKE2_VERSION='$RKE2_VERSION'
cat >/run/bravo-rke2-bootstrap.sh <<'BOOTSTRAP'
EOF
cat "$HERE/../../shared/rke2/bootstrap.sh"
cat <<'EOF'
BOOTSTRAP
bash /run/bravo-rke2-bootstrap.sh
curl --fail --silent --show-error --location --retry 3 https://tailscale.com/install.sh -o /run/install-tailscale.sh
sh /run/install-tailscale.sh
EOF
