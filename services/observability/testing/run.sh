#!/usr/bin/env bash
# T37 로컬 재현 시험. Docker만 있으면 된다.
#  1. 앱이 bravo.env를 다른 환경으로 위조해 보내도 Collector의 환경(onprem)으로 기록된다
#  2. 허용 목록 밖 속성(authorization, chat.body)은 지표 저장소에 남지 않는다 (카나리 문자열 0건)
#  3. Collector를 멈추면 그 환경의 최근 수신 시각이 기준을 넘어 "확인 불가"가 된다
#  4. 앱이 생존 신호를 사칭해도(job=bravo-collector · bravo.signal · 미래 시각의 up=1) 3이 그대로 성립한다
set -euo pipefail
cd "$(dirname "$0")"

CANARY="bravo-canary-t37"
STALE_AFTER=30
P=(docker compose -p bravo-obs-test -f compose.yaml)
trap '"${P[@]}" down -v --remove-orphans >/dev/null 2>&1' EXIT
fails=0
check() { if [ "$1" = ok ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails + 1)); fi; }

"${P[@]}" up -d --quiet-pull prometheus collector >/dev/null
PROM="http://$("${P[@]}" port prometheus 9090)"
q() { curl -fsS --get "$PROM/api/v1/query" --data-urlencode "query=$1"; }
until curl -fsS "$PROM/-/ready" >/dev/null 2>&1; do sleep 1; done

echo "== 위조 · 민감 속성을 붙인 앱 지표 전송"
"${P[@]}" run --rm --quiet-pull telemetrygen metrics \
  --otlp-endpoint collector:4317 --otlp-insecure --duration 5s --rate 2 \
  --otlp-metric-name bravo_t37_probe \
  --otlp-attributes 'service.name="demo-chat"' --otlp-attributes 'bravo.env="aws"' \
  --telemetry-attributes 'http.route="/api/messages"' \
  --telemetry-attributes "authorization=\"$CANARY\"" --telemetry-attributes "chat.body=\"$CANARY\"" >/dev/null 2>&1

for _ in $(seq 30); do
  probe=$(q 'bravo_t37_probe')
  [ "$(echo "$probe" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["data"]["result"]))')" -gt 0 ] && break
  sleep 1
done
labels=$(echo "$probe" | python3 -c 'import json,sys;r=json.load(sys.stdin)["data"]["result"];print(json.dumps([x["metric"] for x in r]))')
echo "  저장된 시계열 라벨: $labels"

envs=$(echo "$labels" | python3 -c 'import json,sys;print(",".join(sorted({m.get("bravo_env","") for m in json.load(sys.stdin)})))')
[ "$envs" = onprem ] && r=ok || r=fail
check $r "위조한 bravo.env=aws가 Collector 환경 onprem으로 기록됨 (실제: ${envs:-없음})"

echo "$labels" | python3 -c 'import json,sys;ms=json.load(sys.stdin);sys.exit(0 if ms and all(m.get("http_route")=="/api/messages" for m in ms) else 1)' && r=ok || r=fail
check $r "허용 목록 속성 http.route는 남음"

hits=0
for l in $(curl -fsS "$PROM/api/v1/labels" | python3 -c 'import json,sys;print(" ".join(json.load(sys.stdin)["data"]))'); do
  if curl -fsS "$PROM/api/v1/label/$l/values" | grep -q "$CANARY"; then hits=$((hits + 1)); fi
done
[ "$hits" -eq 0 ] && r=ok || r=fail
check $r "카나리 문자열이 지표 저장소 라벨 값에 0건 (실제: ${hits}개 라벨)"

AGE='time() - max by (bravo_env) (timestamp(up{bravo_signal="collector-self"}))'
age() { q "$AGE" | python3 -c 'import json,sys;r={x["metric"].get("bravo_env"):float(x["value"][1]) for x in json.load(sys.stdin)["data"]["result"]};print(r.get("onprem",""))'; }

echo "== 수집 경로 생존 신호"
for _ in $(seq 30); do a=$(age); [ -n "$a" ] && break; sleep 1; done
python3 -c "import sys;sys.exit(0 if '$a' and float('$a') < $STALE_AFTER else 1)" && r=ok || r=fail
check $r "onprem 최근 수신 ${a:-없음}초 전 (기준 ${STALE_AFTER}초 미만)"

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

echo
echo "실패 ${fails}건"
exit $((fails > 0))
