#!/usr/bin/env bash
# 리허설 격리 시험용 k3d 클러스터: RKE2(T2)가 준비되기 전에 템플릿과 격리 시험을 돌려 보는 곳.
#   services/rehearsal-runner/scripts/k3d-up.sh        # 생성 (이미 있으면 그대로 둔다)
#   services/rehearsal-runner/scripts/k3d-up.sh down   # 삭제
# kubeconfig에 k3d-<이름> 컨텍스트를 추가한다. 이미 쓰던 현재 컨텍스트가 있으면 바꾸지 않는다
# (kubeconfig가 비어 있었으면 이 컨텍스트가 현재 컨텍스트가 된다).
# CNI는 k3s 기본값(Flannel + 내장 NetworkPolicy 컨트롤러)이다. RKE2(Canal)와 다른 점은 ADR에 남긴다.
set -euo pipefail

name="${K3D_CLUSTER:-bravo-rehearsal}"
image="${K3S_IMAGE:-rancher/k3s:v1.35.5-k3s1}"
context="k3d-$name"

for cmd in docker k3d kubectl; do
  command -v "$cmd" >/dev/null || { echo "$cmd 이 없다." >&2; exit 1; }
done

case "${1:-up}" in
  up)
    if k3d cluster get "$name" >/dev/null 2>&1; then
      echo "클러스터 $name 이 이미 있다."
    else
      k3d cluster create "$name" \
        --image "$image" \
        --servers 1 --agents 1 \
        --k3s-arg "--disable=traefik@server:0" \
        --kubeconfig-update-default \
        --kubeconfig-switch-context=false \
        --wait
    fi
    kubectl --context "$context" wait --for=condition=Ready nodes --all --timeout=120s
    echo "컨텍스트: $context"
    ;;
  down)
    k3d cluster delete "$name"
    ;;
  *)
    echo "사용법: $0 [up|down]" >&2
    exit 1
    ;;
esac
