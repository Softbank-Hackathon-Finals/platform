#!/usr/bin/env bash
# 노드 안에서 root로 실행한다. 새 VM이 T2 기준대로 섰는지 확인하고, 하나라도 어긋나면 종료 코드 1을 낸다.
# check가 함수 이름을 인자로 받아 호출한다.
# shellcheck disable=SC2329
set -u

KUBECTL=(/var/lib/rancher/rke2/bin/kubectl --kubeconfig /etc/rancher/rke2/rke2.yaml)
CIS_SYSCTL=/usr/local/share/rke2/rke2-cis-sysctl.conf
AUDIT_LOG=/var/lib/rancher/rke2/server/logs/audit.log
failed=0

check() {
  local name=$1
  shift
  if "$@" >/dev/null 2>&1; then
    echo "PASS  $name"
  else
    echo "FAIL  $name"
    failed=1
  fi
}

# RKE2가 CIS 프로필 조건으로 배포한 값을 기준으로 삼는다. 버전을 올려 조건이 늘면 여기서 걸린다.
cis_sysctl_applied() {
  local key value
  [ -s "$CIS_SYSCTL" ] || return 1
  while IFS='=' read -r key value; do
    [ "$(sysctl -n "$key")" = "$value" ] || return 1
  done < <(grep -E '^[a-z]' "$CIS_SYSCTL" | tr -d ' ')
}

etcd_account_exists() {
  id -u etcd && getent group etcd && [ "$(getent passwd etcd | cut -d: -f7)" = /usr/sbin/nologin ]
}

rke2_version_pinned() {
  # shellcheck disable=SC1091
  . /etc/bravo-node.env && rke2 --version | grep -qF "$RKE2_VERSION"
}

# 설정 파일이 아니라 실제 동작을 본다. CIS 프로필이 켜져 있으면 일반 네임스페이스에서 특권 Pod가 거부된다.
privileged_pod_denied() {
  "${KUBECTL[@]}" run t2-privileged-check --namespace default --image=registry.k8s.io/pause:3.10 \
    --privileged --dry-run=server 2>&1 | grep -q 'violates PodSecurity'
}

sleep_masked() {
  local target
  for target in sleep suspend hibernate hybrid-sleep; do
    [ "$(systemctl is-enabled "$target.target")" = masked ] || return 1
  done
}

# 정책 파일이 있는지는 보지 않는다. 정책을 넣지 않아도 RKE2가 같은 경로에 level: None 정책을 만든다.
# 이벤트는 응답을 보낸 뒤 파일에 쓰이므로 잠깐 기다린다.
audit_recorded_as_metadata() {
  local name=$1
  for _ in $(seq 1 15); do
    grep -F "\"name\":\"$name\"" "$AUDIT_LOG" | grep -F '"stage":"ResponseComplete"' \
      | grep -qF '"level":"Metadata"' && return 0
    sleep 1
  done
  return 1
}

audit_records_rbac_change() {
  local name
  name=verify-audit-$(date +%s)-$RANDOM
  "${KUBECTL[@]}" create rolebinding "$name" --namespace default --clusterrole=view \
    --user=verify-audit --dry-run=server || return 1
  audit_recorded_as_metadata "$name"
}

audit_omits_secret_value() {
  local name value
  name=verify-audit-$(date +%s)-$RANDOM
  value=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
  "${KUBECTL[@]}" create secret generic "$name" --namespace default \
    --from-literal=probe="$value" --dry-run=server || return 1
  audit_recorded_as_metadata "$name" || return 1
  ! grep -qF -e "$value" -e "$(printf %s "$value" | base64 -w0)" "$AUDIT_LOG"
}

# status 글자만 보면 경고가 있어도 done으로 나온다. 종료 코드는 경고가 있으면 2다.
check 'cloud-init 정상 종료' cloud-init status
check 'CIS sysctl 적용' cis_sysctl_applied
check 'etcd 사용자 · 그룹' etcd_account_exists
check 'CIS 프로필 설정' grep -qx 'profile: cis' /etc/rancher/rke2/config.yaml
check 'RKE2 버전 고정' rke2_version_pinned
check 'rke2-server 실행' systemctl is-active rke2-server
check '노드 Ready' "${KUBECTL[@]}" wait node --all --for=condition=Ready --timeout=300s
check '특권 Pod 거부' privileged_pod_denied
check '감사 로그에 권한 변경 기록' audit_records_rbac_change
check '감사 로그에 Secret 값 없음' audit_omits_secret_value
check '절전 차단' sleep_masked
check 'Tailscale 설치' command -v tailscale

exit "$failed"
