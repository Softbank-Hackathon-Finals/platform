#!/usr/bin/env bash
# 변이 시험: 격리 설정을 일부러 빼고 돌렸을 때 격리 시험이 실패(종료 코드 1)하는지 확인한다.
# 시험이 항상 통과만 하는 가짜가 아님을 보이는 용도. 시험용 네임스페이스를 만들고 끝나면 지운다.
#   services/rehearsal-runner/testing/mutation-check.sh <프로필.env> [--context <컨텍스트>]
set -uo pipefail
[ $# -ge 1 ] || { sed -n '4p' "$0" >&2; exit 2; }
profile="$(realpath "$1")"
shift
cd "$(dirname "$0")/.."
kargs=("$@")
reports="$(mktemp -d)"
k() { kubectl "${kargs[@]}" "$@"; }
check() { uv run --quiet python -m rehearsal_runner.isolation --namespace "$1" --profile "$profile" "${kargs[@]}" --report "$reports/$1.json" >/dev/null; }
failed_ids() { python3 -c 'import json,sys; print(" ".join(sorted({r["id"] for r in json.load(open(sys.argv[1]))["results"] if r["status"] == "fail"})))' "$reports/$1.json"; }

failed=0
expect_breach() {  # 이름, 변이 명령
  local ns="rehearsal-mutation-$1"; shift
  bin/create-and-check "$ns" "$profile" "${kargs[@]}" --report "$reports/$ns-base.json" >/dev/null
  local base=$?
  "$@" "$ns"
  check "$ns"
  local got=$?
  if [ "$base" = 0 ] && [ "$got" = 1 ]; then echo "OK   $ns: 원래 0 → 변이 후 1 (실패한 시험: $(failed_ids "$ns"))"; else echo "FAIL $ns: 원래 $base → 변이 후 $got (기대 0 → 1)"; failed=1; fi
  k delete namespace "$ns" --wait=false >/dev/null
}

no_networkpolicy() { k -n "$1" delete networkpolicy --all >/dev/null; }
no_pod_security() { k label namespace "$1" pod-security.kubernetes.io/enforce- >/dev/null; }

expect_breach netpol no_networkpolicy
expect_breach psa no_pod_security
echo "보고서: $reports"
exit $failed
