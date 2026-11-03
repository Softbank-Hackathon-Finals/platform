#!/usr/bin/env bash
# T37 로컬 재현 시험. Docker와 openssl만 있으면 된다. 인증서는 실행할 때 임시로 만들고 끝나면 지운다.
#  1. 지표 저장소는 이 CA가 발급하고 허용 목록에 있는 이름의 클라이언트 인증서만 받는다 (없음 · 다른 CA · 다른 이름 거부)
#  2. 앱이 bravo.env를 다른 환경으로 위조해 보내도 Collector의 환경(onprem)으로 기록된다
#  3. 허용 목록 밖 속성(authorization, chat.body)은 지표 저장소에 남지 않는다 (카나리 문자열 0건)
#  4. 지표 저장소가 멈춘 동안 받은 지표는 Collector를 재시작해도 디스크 큐에 남아 있다가 전달된다
#  5. Collector를 멈추면 그 환경의 최근 수신 시각이 기준을 넘어 "확인 불가"가 되고 BravoEnvUnobservable 알림이 뜬다
#  6. 앱이 생존 신호를 사칭해도(job=bravo-collector · bravo.signal · 미래 시각의 up=1) 5가 그대로 성립한다
set -euo pipefail
cd "$(dirname "$0")"

CANARY="bravo-canary-t37"
STALE_AFTER=30
P=(docker compose -p bravo-obs-test -f compose.yaml)
CERT_DIR=$(mktemp -d)
export CERT_DIR
trap '"${P[@]}" down -v --remove-orphans >/dev/null 2>&1; rm -rf "$CERT_DIR"' EXIT
fails=0
check() { if [ "$1" = ok ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails + 1)); fi; }

# 시험 전용 CA와 인증서. 컨테이너 사용자(nobody · 10001)가 읽도록 권한을 연다. 시험이 끝나면 지운다.
ca() { openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 -subj "/CN=$1" \
  -keyout "$CERT_DIR/$1.key" -out "$CERT_DIR/$1.crt" 2>/dev/null; }
leaf() { # 이름 CA SAN
  mkdir -p "$CERT_DIR/$1"
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$1" \
    -keyout "$CERT_DIR/$1/tls.key" -out "$CERT_DIR/$1/tls.csr" 2>/dev/null
  openssl x509 -req -days 1 -in "$CERT_DIR/$1/tls.csr" -CA "$CERT_DIR/$2.crt" -CAkey "$CERT_DIR/$2.key" \
    -CAcreateserial -out "$CERT_DIR/$1/tls.crt" \
    -extfile <(printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth,clientAuth\n' "$3") 2>/dev/null
  cp "$CERT_DIR/$2.crt" "$CERT_DIR/$1/ca.crt"
}
ca bravo-ca
ca rogue-ca
leaf prometheus bravo-ca "DNS:prometheus,DNS:localhost,IP:127.0.0.1"
leaf collector-onprem bravo-ca "DNS:collector-onprem"
leaf console-api bravo-ca "DNS:console-api"
leaf intruder bravo-ca "DNS:intruder"
leaf rogue rogue-ca "DNS:collector-onprem"
chmod -R a+rX "$CERT_DIR"

"${P[@]}" up -d --quiet-pull prometheus collector >/dev/null 2>&1
as() { local who=$1; shift; curl -fsS --max-time 5 --cacert "$CERT_DIR/bravo-ca.crt" --cert "$CERT_DIR/$who/tls.crt" --key "$CERT_DIR/$who/tls.key" "$@"; }
q() { as console-api --get "$PROM/api/v1/query" --data-urlencode "query=$1"; }
# 재시작하면 호스트 포트가 바뀌므로 매번 다시 읽는다
ready() {
  for _ in $(seq 60); do
    PROM="https://$("${P[@]}" port prometheus 9090 2>/dev/null)"
    as console-api "$PROM/-/ready" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "  지표 저장소가 60초 안에 준비되지 않음"; exit 1
}
ready
wait_for() { for _ in $(seq "$2"); do r=$(q "$1" || echo '{"data":{"result":[]}}'); [ "$(echo "$r" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["data"]["result"]))')" -gt 0 ] && { echo "$r"; return; }; sleep 1; done; echo "$r"; }
send() { "${P[@]}" run --rm --quiet-pull telemetrygen metrics --otlp-endpoint collector:4317 --otlp-insecure "$@" >/dev/null 2>&1; }

echo "== 지표 저장소 mTLS"
as console-api "$PROM/api/v1/status/buildinfo" >/dev/null 2>&1 && r=ok || r=fail
check $r "허용된 이름(console-api)의 인증서로 조회 성공"
curl -fsS --max-time 5 --cacert "$CERT_DIR/bravo-ca.crt" "$PROM/api/v1/status/buildinfo" >/dev/null 2>&1 && r=fail || r=ok
check $r "클라이언트 인증서 없이는 거부"
as rogue "$PROM/api/v1/status/buildinfo" >/dev/null 2>&1 && r=fail || r=ok
check $r "다른 CA가 발급한 인증서(이름은 collector-onprem)는 거부"
as intruder "$PROM/api/v1/status/buildinfo" >/dev/null 2>&1 && r=fail || r=ok
check $r "같은 CA라도 허용 목록에 없는 이름(intruder)은 거부"

echo "== 위조 · 민감 속성을 붙인 앱 지표 전송"
send --duration 5s --rate 2 --otlp-metric-name bravo_t37_probe \
  --otlp-attributes 'service.name="demo-chat"' --otlp-attributes 'bravo.env="aws"' \
  --telemetry-attributes 'http.route="/api/messages"' \
  --telemetry-attributes "authorization=\"$CANARY\"" --telemetry-attributes "chat.body=\"$CANARY\""
probe=$(wait_for bravo_t37_probe 30)
labels=$(echo "$probe" | python3 -c 'import json,sys;r=json.load(sys.stdin)["data"]["result"];print(json.dumps([x["metric"] for x in r]))')
echo "  저장된 시계열 라벨: $labels"

envs=$(echo "$labels" | python3 -c 'import json,sys;print(",".join(sorted({m.get("bravo_env","") for m in json.load(sys.stdin)})))')
[ "$envs" = onprem ] && r=ok || r=fail
check $r "위조한 bravo.env=aws가 Collector 환경 onprem으로 기록됨 (실제: ${envs:-없음})"

echo "$labels" | python3 -c 'import json,sys;ms=json.load(sys.stdin);sys.exit(0 if ms and all(m.get("http_route")=="/api/messages" for m in ms) else 1)' && r=ok || r=fail
check $r "허용 목록 속성 http.route는 남음"

hits=0
for l in $(as console-api "$PROM/api/v1/labels" | python3 -c 'import json,sys;print(" ".join(json.load(sys.stdin)["data"]))'); do
  if as console-api "$PROM/api/v1/label/$l/values" | grep -q "$CANARY"; then hits=$((hits + 1)); fi
done
[ "$hits" -eq 0 ] && r=ok || r=fail
check $r "카나리 문자열이 지표 저장소 라벨 값에 0건 (실제: ${hits}개 라벨)"

AGE='time() - max by (bravo_env) (timestamp(up{bravo_signal="collector-self"}))'
age() { q "$AGE" | python3 -c 'import json,sys;r={x["metric"].get("bravo_env"):float(x["value"][1]) for x in json.load(sys.stdin)["data"]["result"]};print(r.get("onprem",""))'; }

echo "== 수집 경로 생존 신호"
for _ in $(seq 30); do a=$(age); [ -n "$a" ] && break; sleep 1; done
python3 -c "import sys;sys.exit(0 if '$a' and float('$a') < $STALE_AFTER else 1)" && r=ok || r=fail
check $r "onprem 최근 수신 ${a:-없음}초 전 (기준 ${STALE_AFTER}초 미만)"

echo "== 디스크 큐: 지표 저장소 중지 중 받은 지표를 Collector 재시작 뒤에도 전달"
"${P[@]}" stop prometheus >/dev/null 2>&1
send --duration 5s --rate 2 --otlp-metric-name bravo_t37_queued --otlp-attributes 'service.name="demo-chat"'
sleep 5
"${P[@]}" restart collector >/dev/null 2>&1
sleep 3
"${P[@]}" start prometheus >/dev/null 2>&1
ready
queued=$(wait_for bravo_t37_queued 90 | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["data"]["result"]))')
[ "$queued" -gt 0 ] && r=ok || r=fail
check $r "저장소가 멈췄을 때 보낸 지표가 Collector 재시작 뒤 도착 (시계열 ${queued}개)"

echo "== 앱 경로로 생존 신호 사칭 (20초 뒤 시각의 up=1, job=bravo-collector, bravo.signal) 후 Collector 중지"
OTLP="http://$("${P[@]}" port collector 4318)"
future=$(python3 -c 'import time;print(int((time.time()+20)*1e9))')
curl -fsS -o /dev/null -H 'Content-Type: application/json' "$OTLP/v1/metrics" -d '{"resourceMetrics":[{"resource":{"attributes":[
  {"key":"service.name","value":{"stringValue":"bravo-collector"}},
  {"key":"bravo.signal","value":{"stringValue":"collector-self"}}]},
  "scopeMetrics":[{"metrics":[{"name":"up","gauge":{"dataPoints":[{"asDouble":1,"timeUnixNano":"'"$future"'"}]}}]}]}]}'
sleep 3
"${P[@]}" stop collector >/dev/null 2>&1
# 사칭 표본(중지 약 17초 뒤 시각)이 보이고, 진짜 생존 신호는 30초를 넘긴 시점에 판정한다
sleep 45
forged=$(q 'count(up{job="bravo-collector"} unless up{bravo_signal="collector-self"}) or vector(0)' | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["result"][0]["value"][1])')
[ "$forged" -ge 1 ] && r=ok || r=fail
check $r "사칭 시계열이 저장됐지만 신뢰 라벨이 없음 (${forged}개)"
a=$(age)
if [ -z "$a" ] || python3 -c "import sys;sys.exit(0 if float('$a') > $STALE_AFTER else 1)"; then r=ok; else r=fail; fi
check $r "Collector 중지 45초 뒤 onprem이 확인 불가 (최근 수신: ${a:-없음}초 전)"
firing=$(as console-api "$PROM/api/v1/alerts" | python3 -c 'import json,sys;print(",".join(sorted(a["labels"].get("bravo_env","") for a in json.load(sys.stdin)["data"]["alerts"] if a["labels"]["alertname"]=="BravoEnvUnobservable" and a["state"]=="firing")))')
case ",$firing," in *,onprem,*) r=ok;; *) r=fail;; esac
check $r "BravoEnvUnobservable 알림이 onprem에 대해 firing (firing 환경: ${firing:-없음})"

echo
echo "실패 ${fails}건"
exit $((fails > 0))
