#!/usr/bin/env bash
# T37 인증서 교체 시험. 운영 발급 스크립트로 만든 인증서를 파일만 바꿔(재시작 없이) 교체한다. 절차: docs/observability/certificates.md
#  1. Collector 클라이언트 인증서: 다른 CA 인증서로 바꾸면 전송이 끊기고, 같은 CA의 새 인증서로 바꾸면 돌아온다 (재시작 없음)
#  2. 지표 저장소 서버 인증서: 새 인증서로 바꾸면 다음 연결부터 새 인증서를 낸다 (재시작 없음)
#  3. CA 교체: 옛 · 새 CA 묶음을 거쳐 새 CA로 옮기는 동안 전송이 이어지고, 끝나면 옛 CA 인증서는 거부된다.
#     Collector는 서버를 검증하는 CA 목록(RootCAs)을 시작할 때만 읽으므로(configtls) 묶음 배포 때 한 번 재시작한다
# 판정은 "교체한 시각 이후의 표본이 도착했는가"로 한다(최근 30초 기준만 보면 끊긴 직후를 정상으로 오판한다)
set -euo pipefail
cd "$(dirname "$0")"

STALE_AFTER=30
P=(docker compose -p bravo-obs-rotation -f compose.yaml)
CERT_DIR=$(mktemp -d)
API_TOKEN=unused-in-this-test
BRAVO_TLS_RELOAD_INTERVAL=5s
export CERT_DIR API_TOKEN BRAVO_TLS_RELOAD_INTERVAL
trap '"${P[@]}" down -v --remove-orphans >/dev/null 2>&1; rm -rf "$CERT_DIR"' EXIT
fails=0
check() { if [ "$1" = ok ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails + 1)); fi; }

# shellcheck source=certs.sh
source ./certs.sh
ca bravo-ca
ca rogue-ca
ca bravo-ca-2
leaf prometheus bravo-ca "DNS:prometheus,DNS:localhost,IP:127.0.0.1"
leaf collector-onprem bravo-ca "DNS:collector-onprem"
leaf console-api bravo-ca "DNS:console-api"
leaf console-api-2 bravo-ca-2 "DNS:console-api" console-api
open_for_containers
# 같은 이름의 새 인증서를 임시 위치에 만든 뒤 마운트된 파일을 덮어쓴다(쿠버네티스 Secret 갱신과 같은 효과)
swap() { # 대상디렉터리 CA SAN 이름
  "$ISSUE" leaf "$CERT_DIR/$2" "$4" "$3" "$CERT_DIR/next" 1 >/dev/null
  cp "$CERT_DIR/next/tls.crt" "$CERT_DIR/next/tls.key" "$CERT_DIR/$1/"
  chmod a+r "$CERT_DIR/$1/tls.key"
}

"${P[@]}" up -d --quiet-pull prometheus collector >/dev/null 2>&1
PROM="https://$("${P[@]}" port prometheus 9090)"
# 조회 클라이언트가 믿는 CA(CA)와 쓰는 인증서(CLIENT)는 교체 단계마다 바뀐다
CA=bravo-ca
CLIENT=console-api
as() { curl -fsS --max-time 5 --cacert "$CERT_DIR/$CA.crt" --cert "$CERT_DIR/$1/tls.crt" --key "$CERT_DIR/$1/tls.key" "${@:2}"; }
q() { as "$CLIENT" --get "$PROM/api/v1/query" --data-urlencode "query=$1"; }
for _ in $(seq 60); do as console-api "$PROM/-/ready" >/dev/null 2>&1 && break; sleep 1; done
age() { q 'time() - max by (bravo_env) (timestamp(up{bravo_signal="collector-self"}))' 2>/dev/null | python3 -c 'import json,sys;r={x["metric"].get("bravo_env"):float(x["value"][1]) for x in json.load(sys.stdin)["data"]["result"]};print(r.get("onprem",""))' 2>/dev/null || true; }
fresh() { a=$(age); [ -n "$a" ] && python3 -c "import sys;sys.exit(0 if float('$a') < $STALE_AFTER else 1)"; }
wait_fresh() { for _ in $(seq "$1"); do fresh && return 0; sleep 2; done; return 1; }
wait_stale() { for _ in $(seq "$1"); do fresh || return 0; sleep 2; done; return 1; }
# 시각 T(초) 이후에 찍힌 생존 신호 표본이 저장소에 도착했는가
last_ts() { q 'max by (bravo_env) (timestamp(up{bravo_signal="collector-self"}))' 2>/dev/null | python3 -c 'import json,sys;r={x["metric"].get("bravo_env"):float(x["value"][1]) for x in json.load(sys.stdin)["data"]["result"]};print(r.get("onprem",0))' 2>/dev/null || echo 0; }
delivered_after() { for _ in $(seq 25); do python3 -c "import sys;sys.exit(0 if float('$(last_ts)') > $1 else 1)" && return 0; sleep 2; done; return 1; }
now() { date +%s; }
started() { docker inspect -f '{{.State.StartedAt}}' "$("${P[@]}" ps -q "$1")"; }
serial() { openssl s_client -connect "${PROM#https://}" -servername localhost -CAfile "$CERT_DIR/$1.crt" \
  -cert "$CERT_DIR/$2/tls.crt" -key "$CERT_DIR/$2/tls.key" </dev/null 2>/dev/null | openssl x509 -noout -serial 2>/dev/null; }

wait_fresh 30 && r=ok || r=fail
check $r "시작: onprem 수집 경로 정상"
c0=$(started collector); p0=$(started prometheus)

echo "== 1. Collector 클라이언트 인증서 교체 (재시작 없음, reload_interval 5초)"
swap collector-onprem rogue-ca "DNS:collector-onprem" collector-onprem
wait_stale 40 && r=ok || r=fail
check $r "다른 CA 인증서로 바꾸면 저장소가 거부해 onprem이 확인 불가가 됨 (Collector가 새 파일을 읽었다는 증거)"
t=$(now); swap collector-onprem bravo-ca "DNS:collector-onprem" collector-onprem
delivered_after "$t" && r=ok || r=fail
check $r "같은 CA의 새 인증서로 바꾸면 전송이 돌아옴(교체 뒤 표본 도착)"
[ "$(started collector)" = "$c0" ] && r=ok || r=fail
check $r "Collector는 재시작하지 않았음"

echo "== 2. 지표 저장소 서버 인증서 교체 (재시작 없음)"
s1=$(serial bravo-ca console-api)
t=$(now)
swap prometheus bravo-ca "DNS:prometheus,DNS:localhost,IP:127.0.0.1" prometheus
s2=$(serial bravo-ca console-api)
[ -n "$s1" ] && [ -n "$s2" ] && [ "$s1" != "$s2" ] && r=ok || r=fail
check $r "다음 연결부터 새 서버 인증서를 냄 ($s1 → $s2)"
delivered_after "$t" && r=ok || r=fail
check $r "서버 인증서 교체 뒤에도 Collector 전송 정상(교체 뒤 표본 도착)"

echo "== 3. CA 교체: 옛+새 묶음 → 새 인증서 발급 → 옛 CA 제거"
cat "$CERT_DIR/bravo-ca.crt" "$CERT_DIR/bravo-ca-2.crt" > "$CERT_DIR/bundle.crt"
# (a) 모든 쪽이 두 CA를 함께 신뢰한다
cp "$CERT_DIR/bundle.crt" "$CERT_DIR/prometheus/ca.crt"
cp "$CERT_DIR/bundle.crt" "$CERT_DIR/collector-onprem/ca.crt"
# Collector의 서버 검증 CA 목록은 재시작해야 바뀐다(쿠버네티스에서는 rollout restart). 계획된 재시작 1회
"${P[@]}" restart collector >/dev/null 2>&1
c1=$(started collector)
t=$(now)
delivered_after "$t" && r=ok || r=fail
check $r "(a) 묶음을 받은 Collector가 재시작 뒤 전송 재개"
as console-api-2 "$PROM/api/v1/status/buildinfo" >/dev/null 2>&1 && r=ok || r=fail
check $r "(a) 묶음 적용 직후 새 CA 클라이언트 인증서도 받음 (재시작 없음)"
as console-api "$PROM/api/v1/status/buildinfo" >/dev/null 2>&1 && r=ok || r=fail
check $r "(a) 옛 CA 클라이언트 인증서도 계속 받음"
# (b) 새 CA로 다시 발급한다
swap collector-onprem bravo-ca-2 "DNS:collector-onprem" collector-onprem
cp "$CERT_DIR/bundle.crt" "$CERT_DIR/collector-onprem/ca.crt"
t=$(now)
swap prometheus bravo-ca-2 "DNS:prometheus,DNS:localhost,IP:127.0.0.1" prometheus
cp "$CERT_DIR/bundle.crt" "$CERT_DIR/prometheus/ca.crt"
CA=bundle   # 서버 인증서가 새 CA로 바뀌었으므로 조회 클라이언트도 묶음을 믿는다
delivered_after "$t" && r=ok || r=fail
check $r "(b) Collector · 저장소를 새 CA 인증서로 바꾼 뒤에도 전송 정상"
# (c) 옛 CA를 신뢰 목록에서 뺀다
t=$(now)
cp "$CERT_DIR/bravo-ca-2.crt" "$CERT_DIR/prometheus/ca.crt"
CA=bravo-ca-2
as console-api "$PROM/api/v1/status/buildinfo" >/dev/null 2>&1 && r=fail || r=ok
check $r "(c) 옛 CA 클라이언트 인증서는 이제 거부"
as console-api-2 "$PROM/api/v1/status/buildinfo" >/dev/null 2>&1 && r=ok || r=fail
check $r "(c) 새 CA 클라이언트 인증서는 받음"
CLIENT=console-api-2
delivered_after "$t" && r=ok || r=fail
check $r "(c) 옛 CA 제거 뒤에도 Collector 전송 정상"
[ "$(started collector)" = "$c1" ] && [ "$c1" != "$c0" ] && [ "$(started prometheus)" = "$p0" ] && r=ok || r=fail
check $r "재시작은 (a)의 Collector 1회뿐, 저장소는 재시작 없음"

echo
echo "실패 ${fails}건"
exit $((fails > 0))
