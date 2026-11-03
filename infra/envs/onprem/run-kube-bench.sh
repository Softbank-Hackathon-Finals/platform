#!/usr/bin/env bash
# 노드 안에서 root로 실행한다. kube-bench로 CIS 점검을 돌려 항목별 결과를 JSON으로 남기고 요약을 출력한다.
# kube-bench 0.16.0의 RKE2 기준 중 가장 새것은 rke2-cis-1.9다. RKE2 버전보다 오래된 기준이라 결과는 항목별로 읽는다.
set -euo pipefail

VERSION=0.16.0
BENCHMARK=rke2-cis-1.9
RESULT=/var/log/kube-bench.json

case "$(uname -m)" in
  aarch64) ARCH=arm64 SHA256=64500561f5fcaa3f86fe951ed26bbfc28f7bbf3d2eac13843abfd2924955d10b ;;
  x86_64) ARCH=amd64 SHA256=82dbc7e598740dc9344d41f8ad0b8210d57c4c00bdb2c5f1d8a69a2b98baddcf ;;
  *)
    echo "지원하지 않는 CPU: $(uname -m)" >&2
    exit 1
    ;;
esac

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
TARBALL=kube-bench_${VERSION}_linux_$ARCH.tar.gz
curl -fsSL --retry 5 -o "$WORK/$TARBALL" \
  "https://github.com/aquasecurity/kube-bench/releases/download/v$VERSION/$TARBALL"
echo "$SHA256  $WORK/$TARBALL" | sha256sum -c --quiet
tar -xzf "$WORK/$TARBALL" -C "$WORK"

PATH=/var/lib/rancher/rke2/bin:$PATH KUBECONFIG=/etc/rancher/rke2/rke2.yaml \
  "$WORK/kube-bench" run --config-dir "$WORK/cfg" --benchmark "$BENCHMARK" \
  --json --outputfile "$RESULT"

python3 - "$RESULT" <<'PY'
import json
import sys

report = json.load(open(sys.argv[1]))
print(f"pass {report['Totals']['total_pass']} / fail {report['Totals']['total_fail']} / "
      f"warn {report['Totals']['total_warn']} / info {report['Totals']['total_info']}")
for control in report["Controls"]:
    for test in control["tests"]:
        for result in test["results"]:
            if result["status"] == "FAIL":
                print(f"FAIL {result['test_number']} {result['test_desc']}")
PY
echo "전체 결과: $RESULT"
