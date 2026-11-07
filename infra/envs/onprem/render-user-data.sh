#!/usr/bin/env bash
# 새 VM에 넣을 cloud-init user-data를 표준 출력으로 낸다. Terraform이 없는 온프렘에서 T7의 templatefile과 같은 일을 한다.
# RKE2 · CIS 기준은 infra/shared/rke2/bootstrap.sh 한 곳에 두고, 여기에는 노드 파일(T44 IaC 묶음)과 Tailscale 설치를 더한다.
# 노드 파일은 infra/iac/nodes/<환경>/manifest.json 그대로 조립한다(감사 정책 등). 환경은 BRAVO_NODE_ENV(기본 target-dev).
# 비밀 값을 넣지 않는다. user-data는 VM 안에 그대로 남는다. Tailscale 로그인은 T30에서 한다.
set -euo pipefail

# 출발지 EKS(T6)와 플랫폼 RKE2(T7)의 Kubernetes 1.35에 맞춘다.
RKE2_VERSION=v1.35.9+rke2r1
HERE=$(cd "$(dirname "$0")" && pwd)
NODES="$HERE/../../iac/nodes/${BRAVO_NODE_ENV:-target-dev}"

# 노드 파일(감사 정책 포함)은 RKE2가 처음 뜨기 전에 있어야 한다. 감사 정책 파일이 없으면 RKE2가 같은 경로에
# level: None 정책을 직접 만든다. 묶음의 해시가 manifest와 다르면 아무것도 내지 않고 멈춘다.
NODE_FILES=$(python3 - "$NODES" <<'ASSEMBLE'
import hashlib, json, shlex, sys
from pathlib import Path
# The only node files root may write at first boot (same set as infra/iac/nodes/apply.py ALLOWED).
ALLOWED = {"/etc/rancher/rke2/audit-policy.yaml": "0600"}
root = Path(sys.argv[1])
manifest = json.loads((root / "manifest.json").read_text())
out = []
for n, row in enumerate(manifest["files"]):
    if ALLOWED.get(row["path"]) != row["mode"]:
        sys.exit("node file path or mode is not allowed: " + row["path"])
    data = (root / row["path"].lstrip("/")).read_bytes()
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        sys.exit("node bundle does not match its manifest: " + row["path"])
    if not data.endswith(b"\n"):  # the heredoc terminator must start its own line
        sys.exit("node file must end with a newline: " + row["path"])
    path = shlex.quote(row["path"])
    out.append(f"install -d {shlex.quote(str(Path(row['path']).parent))}")
    out.append(f"install -m {row['mode'][1:]} /dev/stdin {path} <<'BRAVO_NODE_FILE_{n}'")
    out.append(data.decode() + f"BRAVO_NODE_FILE_{n}")
print("\n".join(out))
ASSEMBLE
)
cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
echo 'RKE2_VERSION=$RKE2_VERSION' >/etc/bravo-node.env
export RKE2_VERSION='$RKE2_VERSION'
install -d -m 700 /etc/rancher /etc/rancher/rke2
EOF
printf '%s\n' "$NODE_FILES"
cat <<'EOF'
cat >/run/bravo-rke2-bootstrap.sh <<'BOOTSTRAP'
EOF
cat "$HERE/../../shared/rke2/bootstrap.sh"
cat <<'EOF'
BOOTSTRAP
bash /run/bravo-rke2-bootstrap.sh
curl --fail --silent --show-error --location --retry 3 https://tailscale.com/install.sh -o /run/install-tailscale.sh
sh /run/install-tailscale.sh
EOF
