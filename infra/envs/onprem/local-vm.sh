#!/usr/bin/env bash
# Mac에서 arm64 또는 x86_64 Ubuntu VM에 같은 render-user-data.sh를 넣는다.
# BRAVO_VM_ARCH=arm64|x86_64 (기본: 호스트 CPU). brew install qemu가 필요하다.
#
#   ./local-vm.sh up             새 VM을 만들고 cloud-init이 끝날 때까지 기다린다
#   ./local-vm.sh run <스크립트>  VM 안에서 스크립트를 root로 실행한다 (예: verify-node.sh)
#   ./local-vm.sh ssh [명령]      VM에 접속하거나 명령 하나를 실행한다
#   ./local-vm.sh down           VM과 디스크를 지운다 (내려받은 이미지는 남긴다)
#
# 다른 user-data로 시험하려면 USER_DATA=<파일> ./local-vm.sh up
set -euo pipefail
umask 077

HERE=$(cd "$(dirname "$0")" && pwd)
HOST_ARCH=$(uname -m)
case "$HOST_ARCH" in arm64|aarch64) HOST_ARCH=arm64 ;; x86_64|amd64) HOST_ARCH=x86_64 ;; esac
case "${BRAVO_VM_ARCH:-$HOST_ARCH}" in
  arm64|aarch64)
    ARCH=arm64 IMAGE_ARCH=arm64 MACHINE=virt QEMU=qemu-system-aarch64 CPU=cortex-a72
    IMAGE_SHA256=50e24e1fb79e89116e939d90aa7738f7af48c442fa1bc2510131e0b39c50348e
    SUFFIX= DEFAULT_SSH_PORT=2222 DEFAULT_API_PORT=6443
    ;;
  x86_64|amd64)
    ARCH=x86_64 IMAGE_ARCH=amd64 MACHINE=q35 QEMU=qemu-system-x86_64 CPU=max
    IMAGE_SHA256=04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94
    SUFFIX=-x86_64 DEFAULT_SSH_PORT=2223 DEFAULT_API_PORT=6444
    ;;
  *) echo 'BRAVO_VM_ARCH must be arm64 or x86_64' >&2; exit 2 ;;
esac
ACCEL=tcg,thread=multi
if [ "$ARCH" = "$HOST_ARCH" ]; then ACCEL=hvf; CPU=host; fi
STATE=${BRAVO_VM_STATE:-${XDG_CACHE_HOME:-$HOME/.cache}/bravo-onprem-vm$SUFFIX}
VM=$STATE/vm
IMAGE_BUILD=release-20261017
IMAGE_NAME=ubuntu-24.04-server-cloudimg-$IMAGE_ARCH.img
SSH_PORT=${BRAVO_SSH_PORT:-$DEFAULT_SSH_PORT}
API_PORT=${BRAVO_API_PORT:-$DEFAULT_API_PORT}
export BRAVO_VM_ARCH=$ARCH BRAVO_SSH_PORT=$SSH_PORT BRAVO_API_PORT=$API_PORT
if [ "${1:-}" = ssh ] || [ "${1:-}" = run ]; then
  SSH_PORT=$(python3 "$HERE/local_vm.py" connection "$STATE")
fi
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
  local platform_args=(-M "$MACHINE" -accel "$ACCEL" -cpu "$CPU")
  if [ "$ARCH" = arm64 ]; then platform_args+=(-bios "$(brew --prefix)/share/qemu/edk2-aarch64-code.fd"); fi
  qemu-img create -q -f qcow2 -F qcow2 -b "$STATE/$IMAGE_NAME" "$VM/disk.qcow2" 40G
  "$QEMU" "${platform_args[@]}" -smp 4 -m 8192 \
    -drive "file=$VM/disk.qcow2,if=virtio" \
    -drive "file=$VM/seed.iso,if=virtio,format=raw,readonly=on" \
    -nic "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22,hostfwd=tcp:127.0.0.1:$API_PORT-:6443" \
    -display none -serial "file:$VM/console.log" -daemonize -pidfile "$VM/qemu.pid" \
    -uuid "$1" -qmp "unix:$VM/qmp.sock,server=on,wait=off"
}

wait_for_cloud_init() {
  local tries=0
  until "${SSH[@]}" true 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -gt 180 ]; then
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

prepare() {
  fetch_image
  write_seed
  start_qemu "$1"
}

wait_ready() {
  SECONDS=0
  wait_for_cloud_init
  echo "cloud-init 완료: ${SECONDS}초"
}

case "${1:-}" in
  up|down) exec python3 "$HERE/local_vm.py" "$1" "$STATE" ;;
  _prepare) prepare "${2:?launch UUID required}" ;;
  _wait) wait_ready ;;
  run) "${SSH[@]}" sudo bash -s <"$2" ;;
  ssh)
    shift
    exec "${SSH[@]}" "$@"
    ;;
  *)
    sed -n '5,8p' "$0"
    exit 2
    ;;
esac
