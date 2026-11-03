#!/usr/bin/env bash
# Apple Silicon Mac에서 새 Ubuntu VM을 QEMU로 띄워 render-user-data.sh의 결과를 넣는다.
# 온프렘 데모 머신이 정해지기 전 시험과 T3 · T9 개발에 쓴다. brew install qemu가 필요하다.
#
#   ./local-vm.sh up             새 VM을 만들고 cloud-init이 끝날 때까지 기다린다
#   ./local-vm.sh run <스크립트>  VM 안에서 스크립트를 root로 실행한다 (예: verify-node.sh)
#   ./local-vm.sh ssh [명령]      VM에 접속하거나 명령 하나를 실행한다
#   ./local-vm.sh down           VM과 디스크를 지운다 (내려받은 이미지는 남긴다)
#
# 다른 user-data로 시험하려면 USER_DATA=<파일> ./local-vm.sh up
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
STATE=${XDG_CACHE_HOME:-$HOME/.cache}/bravo-onprem-vm
VM=$STATE/vm
IMAGE_BUILD=release-20261017
IMAGE_NAME=ubuntu-24.04-server-cloudimg-arm64.img
IMAGE_SHA256=50e24e1fb79e89116e939d90aa7738f7af48c442fa1bc2510131e0b39c50348e
SSH_PORT=2222
SSH=(ssh -i "$VM/id_ed25519" -p "$SSH_PORT" -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=3 ubuntu@127.0.0.1)

fetch_image() {
  local image=$STATE/$IMAGE_NAME
  mkdir -p "$STATE"
  if [ ! -f "$image" ]; then
    curl -fL --retry 5 -o "$image.part" \
      "https://cloud-images.ubuntu.com/releases/noble/$IMAGE_BUILD/$IMAGE_NAME"
    mv "$image.part" "$image"
  fi
  echo "$IMAGE_SHA256  $image" | shasum -a 256 -c --quiet
}

# 접속 키는 VM마다 새로 만들고 메타데이터로 넣는다. EC2와 같은 경로라 user-data에 키를 적지 않아도 된다.
write_seed() {
  mkdir -p "$VM/seed"
  ssh-keygen -q -t ed25519 -N '' -f "$VM/id_ed25519"
  if [ -n "${USER_DATA:-}" ]; then
    cp "$USER_DATA" "$VM/seed/user-data"
  else
    "$HERE/render-user-data.sh" >"$VM/seed/user-data"
  fi
  cat >"$VM/seed/meta-data" <<EOF
instance-id: bravo-$(date +%s)
local-hostname: bravo-onprem
public-keys:
  - $(cat "$VM/id_ed25519.pub")
EOF
  hdiutil makehybrid -quiet -iso -joliet -default-volume-name cidata -o "$VM/seed.iso" "$VM/seed"
}

start_qemu() {
  qemu-img create -q -f qcow2 -F qcow2 -b "$STATE/$IMAGE_NAME" "$VM/disk.qcow2" 40G
  qemu-system-aarch64 -M virt -accel hvf -cpu host -smp 4 -m 8192 \
    -bios "$(brew --prefix)/share/qemu/edk2-aarch64-code.fd" \
    -drive "file=$VM/disk.qcow2,if=virtio" \
    -drive "file=$VM/seed.iso,if=virtio,format=raw,readonly=on" \
    -nic "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22,hostfwd=tcp:127.0.0.1:6443-:6443" \
    -display none -serial "file:$VM/console.log" -daemonize -pidfile "$VM/qemu.pid"
}

wait_for_cloud_init() {
  local tries=0
  until "${SSH[@]}" true 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -gt 60 ]; then
      echo "VM에 접속하지 못했다. 부팅 기록: $VM/console.log" >&2
      exit 1
    fi
    sleep 5
  done
  if ! "${SSH[@]}" cloud-init status --wait; then
    "${SSH[@]}" sudo tail -n 30 /var/log/cloud-init-output.log >&2
    echo "cloud-init이 실패했다." >&2
    exit 1
  fi
}

up() {
  if [ -e "$VM" ]; then
    echo "이미 VM이 있다. 먼저 down을 실행한다." >&2
    exit 1
  fi
  fetch_image
  write_seed
  start_qemu
  SECONDS=0
  wait_for_cloud_init
  echo "cloud-init 완료: ${SECONDS}초"
}

down() {
  if [ -f "$VM/qemu.pid" ]; then
    kill "$(cat "$VM/qemu.pid")" 2>/dev/null || true
  fi
  rm -rf "$VM"
}

case "${1:-}" in
  up) up ;;
  run) "${SSH[@]}" sudo bash -s <"$2" ;;
  ssh)
    shift
    exec "${SSH[@]}" "$@"
    ;;
  down) down ;;
  *)
    sed -n '5,8p' "$0"
    exit 2
    ;;
esac
