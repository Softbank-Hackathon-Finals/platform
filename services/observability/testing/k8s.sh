#!/usr/bin/env bash
# T37 쿠버네티스 배포 시험. Docker 안에 k3s(T7 RKE2와 같은 쿠버네티스 1.35)를 띄우고 deploy/collector · deploy/platform을 그대로 적용한다.
# 한 클러스터가 플랫폼과 목적지(onprem) 역할을 함께 한다. 끝나면 클러스터와 인증서를 지운다.
#  1. 두 매니페스트가 Pod 보안 표준 restricted 네임스페이스에서 뜬다 (비루트 · 읽기 전용 루트 · 권한 제거)
#  2. 라벨(bravo.observability/send=true)을 붙인 네임스페이스의 앱 지표만 Collector를 거쳐 mTLS로 저장소에 도착한다
#  3. 라벨이 없는 네임스페이스는 NetworkPolicy가 막는다 (Collector로 보내기, 지표 저장소 연결 모두)
#  4. Collector를 0개로 줄이면 BravoEnvUnobservable 알림이 onprem에 대해 뜬다
#  5. 콘솔용 조회 API가 restricted로 뜨고, platform-client 라벨 네임스페이스에서만 호출된다(토큰 필수)
#  6. DB 오버레이: T6과 같은 CNPG(오퍼레이터 1.30.1, PostgreSQL 18.4)의 지표가 최소 권한으로 수집되고 조회 API DB 뷰에 보인다
#     DB 긁기 경로의 지표에만 출처 라벨(bravo_signal=cnpg-scrape)이 붙고, 앱이 같은 이름의 지표를 보내도 DB 뷰에 들어오지 않는다
#  7. 창 수신(T36)이 restricted로 뜨고, window-producer 라벨 네임스페이스의 수신 토큰만 받으며, 밖으로 나가지 못한다
# helm이 필요하다(CNPG 차트).
set -euo pipefail
cd "$(dirname "$0")"

CNPG_IMAGE=1.30.1@sha256:923c267ec29636db3bee20f993d0ec4973fa22998e1adad37da79e4d32b5bc07
PG_IMAGE=ghcr.io/cloudnative-pg/postgresql:18.4-system-trixie@sha256:42708a75345b7a48fdd9257b071830783a97fd228529196b6313187a7198e185
K3S=rancher/k3s:v1.35.9-k3s1@sha256:ec9868c6a38d4e8c1869832fb5fd1eb8473c39794a0b44d2b952e7ba911951bc
GEN=ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:v0.164.0@sha256:b536c7220c5ea587f04dff6c5e6eecb48e40b5b58ce70690b15734aae98d8321
NAME="bravo-obs-k3s-$(openssl rand -hex 6)"
API_IMAGE="docker.io/library/${NAME}-api:dev"
CONTAINER_ID=""
NS=bravo-observability
CERT_DIR=$(mktemp -d)
KUBECONFIG="$CERT_DIR/kubeconfig"
API_TOKEN=$(openssl rand -hex 24)
WINDOW_TOKEN=$(openssl rand -hex 24)
export CERT_DIR KUBECONFIG
PF=""
# Port-forward termination must not abort cleanup; remove only the container ID this run created.
cleanup() {
  if [ -n "$PF" ]; then kill "$PF" 2>/dev/null || true; wait "$PF" 2>/dev/null || true; fi
  if [ -n "$CONTAINER_ID" ]; then docker rm -f "$CONTAINER_ID" >/dev/null 2>&1 || true; fi
  rm -rf "$CERT_DIR"
}
trap cleanup EXIT
fails=0
check() { if [ "$1" = ok ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails + 1)); fi; }

# shellcheck source=certs.sh
source ./certs.sh
ca bravo-ca
leaf prometheus bravo-ca "DNS:prometheus,DNS:bravo-prometheus.$NS.svc,DNS:bravo-prometheus.$NS.svc.cluster.local,DNS:localhost,IP:127.0.0.1"
leaf collector-onprem bravo-ca "DNS:collector-onprem"
leaf console-api bravo-ca "DNS:console-api"

echo "== k3s 시작"
CONTAINER_ID=$(docker run -d --privileged --name "$NAME" -p 127.0.0.1::6443 --tmpfs /run --tmpfs /var/run \
  "$K3S" server --disable=traefik --disable=metrics-server --disable=servicelb --tls-san=127.0.0.1)
for _ in $(seq 60); do docker exec "$NAME" test -f /etc/rancher/k3s/k3s.yaml 2>/dev/null && break; sleep 2; done
docker exec "$NAME" cat /etc/rancher/k3s/k3s.yaml | sed "s#https://127.0.0.1:6443#https://$(docker port "$NAME" 6443)#" > "$KUBECONFIG"
for _ in $(seq 60); do kubectl get nodes 2>/dev/null | grep -q " Ready" && break; sleep 2; done
kubectl wait --for=condition=Ready node --all --timeout=120s >/dev/null
# 조회 API 이미지를 로컬에서 빌드해 k3s에 넣는다(레지스트리를 쓰지 않음)
docker build -q -t "$API_IMAGE" -f ../Dockerfile ../../.. >/dev/null
docker save "$API_IMAGE" | docker exec -i "$NAME" ctr -n k8s.io images import - >/dev/null

echo "== CNPG (T6 출발지와 같은 버전)"
kubectl create namespace cnpg-system >/dev/null
kubectl create namespace source-dev >/dev/null
helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null 2>&1 || true
helm install cnpg cnpg/cloudnative-pg --version 0.29.1 -n cnpg-system --set image.tag="$CNPG_IMAGE" \
  --set config.data.WATCH_NAMESPACE=source-dev --wait --timeout 5m >/dev/null
kubectl apply -f - >/dev/null <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: {name: source-db, namespace: source-dev}
spec: {instances: 1, imageName: "$PG_IMAGE", storage: {size: 1Gi}}
EOF

echo "== 배포"
# Secret · ConfigMap을 먼저 넣으려고 네임스페이스를 미리 만든다(뒤의 apply와 충돌하지 않게 create).
kubectl create namespace "$NS" --save-config >/dev/null
for who in prometheus collector-onprem; do
  secret=$([ "$who" = prometheus ] && echo bravo-prometheus-tls || echo bravo-collector-tls)
  kubectl -n "$NS" create secret generic "$secret" --from-file="$CERT_DIR/$who/ca.crt" \
    --from-file="$CERT_DIR/$who/tls.crt" --from-file="$CERT_DIR/$who/tls.key" >/dev/null
done
kubectl -n "$NS" create secret generic bravo-observability-api-tls --from-file="$CERT_DIR/console-api/ca.crt" \
  --from-file="$CERT_DIR/console-api/tls.crt" --from-file="$CERT_DIR/console-api/tls.key" >/dev/null
kubectl -n "$NS" create secret generic bravo-observability-api-token --from-literal=token="$API_TOKEN" >/dev/null
kubectl -n "$NS" create secret generic bravo-window-intake-token --from-literal=token="$WINDOW_TOKEN" >/dev/null
kubectl -n "$NS" create configmap bravo-window-intake --from-literal=plans=plan-k8s >/dev/null
kubectl -n "$NS" create configmap bravo-collector-env --from-literal=BRAVO_ENV=onprem \
  --from-literal=PLATFORM_OTLP_ENDPOINT="https://bravo-prometheus.$NS.svc:9090/api/v1/otlp" \
  --from-literal=BRAVO_DB_NAMESPACE=source-dev >/dev/null
kubectl kustomize k8s-overlay | sed "s#docker.io/library/bravo-observability-api:dev#$API_IMAGE#g" | kubectl apply -f - >/dev/null
kubectl apply -k ../deploy/collector-db >/dev/null
kubectl apply -k ../deploy/collector-db-access >/dev/null
r=ok
kubectl -n "$NS" rollout status deploy/bravo-prometheus --timeout=240s >/dev/null || r=fail
kubectl -n "$NS" rollout status deploy/bravo-collector --timeout=240s >/dev/null || r=fail
kubectl -n "$NS" rollout status deploy/bravo-observability-api --timeout=240s >/dev/null || r=fail
kubectl -n "$NS" rollout status deploy/bravo-window-intake --timeout=240s >/dev/null || r=fail
check $r "restricted 네임스페이스에서 지표 저장소 · Collector · 조회 API · 창 수신이 준비됨"

kubectl -n "$NS" port-forward svc/bravo-prometheus :9090 >"$CERT_DIR/port-forward.log" 2>&1 &
PF=$!
for _ in $(seq 30); do
  PORT=$(sed -n 's/^Forwarding from 127.0.0.1:\([0-9]*\) -> 9090$/\1/p' "$CERT_DIR/port-forward.log")
  [ -z "$PORT" ] || break
  kill -0 "$PF" 2>/dev/null || { echo "port-forward failed"; exit 1; }
  sleep 1
done
[ -n "$PORT" ] || { echo "port-forward timed out"; exit 1; }
PROM="https://127.0.0.1:$PORT"
as() { curl -fsS --max-time 5 --cacert "$CERT_DIR/bravo-ca.crt" --cert "$CERT_DIR/console-api/tls.crt" --key "$CERT_DIR/console-api/tls.key" "$@"; }
q() { as --get "$PROM/api/v1/query" --data-urlencode "query=$1"; }
for _ in $(seq 30); do as "$PROM/-/ready" >/dev/null 2>&1 && break; sleep 1; done
count() { q "$1" 2>/dev/null | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["data"]["result"]))' 2>/dev/null || echo 0; }

echo "== 앱 네임스페이스에서 지표 전송"
for ns in app stranger; do kubectl create namespace "$ns" >/dev/null; done
kubectl label namespace app bravo.observability/send=true >/dev/null
for ns in app stranger; do
  kubectl -n "$ns" run sender --image="$GEN" --restart=Never -- metrics \
    --otlp-endpoint "bravo-collector.$NS.svc:4317" --otlp-insecure --duration 10s --rate 2 \
    --otlp-metric-name "bravo_k8s_$ns" --otlp-attributes 'service.name="demo-chat"' >/dev/null
done
for _ in $(seq 90); do [ "$(count bravo_k8s_app)" -gt 0 ] && break; sleep 2; done
[ "$(count bravo_k8s_app)" -gt 0 ] && r=ok || r=fail
check $r "라벨을 붙인 네임스페이스(app)의 지표가 Collector → mTLS → 저장소로 도착"
envs=$(q bravo_k8s_app | python3 -c 'import json,sys;print(",".join(sorted({x["metric"].get("bravo_env","") for x in json.load(sys.stdin)["data"]["result"]})))')
[ "$envs" = onprem ] && r=ok || r=fail
check $r "도착한 지표의 환경은 Collector 설정값 onprem (실제: ${envs:-없음})"
sleep 20
[ "$(count bravo_k8s_stranger)" -eq 0 ] && r=ok || r=fail
check $r "라벨이 없는 네임스페이스(stranger)의 지표는 NetworkPolicy가 막아 도착하지 않음"

# 같은 연결 시험을 허용 대조군(platform-client 라벨)과 함께 돌려, 거부가 DNS · 이미지 문제가 아니라 정책 때문임을 확인한다
kubectl create namespace console >/dev/null
kubectl label namespace console bravo.observability/platform-client=true >/dev/null
probe() {
  kubectl -n "$1" run probe --image=busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec \
    --restart=Never -- nc -z -w 5 "bravo-prometheus.$NS.svc" 9090 >/dev/null
  for _ in $(seq 30); do p=$(kubectl -n "$1" get pod probe -o jsonpath='{.status.phase}'); case $p in Succeeded|Failed) break;; esac; sleep 2; done
  echo "$p"
}
allowed=$(probe console)
kubectl -n console delete pod probe --wait=true >/dev/null
denied=$(probe stranger)
[ "$allowed" = Succeeded ] && r=ok || r=fail
check $r "대조군: platform-client 라벨 네임스페이스(console)에서는 지표 저장소 9090 연결 성공 (probe: ${allowed:-없음})"
[ "$denied" = Failed ] && r=ok || r=fail
check $r "라벨이 없는 네임스페이스(stranger)에서 지표 저장소 9090 연결은 NetworkPolicy가 막음 (probe: ${denied:-없음})"

echo "== 조회 API"
call() { # 네임스페이스 토큰 이름 [경로]
  kubectl -n "$1" run "call-$3" --image=busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec \
    --restart=Never -- wget -q -T 5 -O - --header "Authorization: Bearer $2" "http://bravo-observability-api.$NS.svc:8080${4:-/v1/observability/environments}" >/dev/null
  for _ in $(seq 30); do p=$(kubectl -n "$1" get pod "call-$3" -o jsonpath='{.status.phase}'); case $p in Succeeded|Failed) break;; esac; sleep 2; done
  echo "$p"
}
phase=$(call console "$API_TOKEN" ok)
body=$(kubectl -n console logs call-ok 2>/dev/null || true)
state=$(echo "$body" | python3 -c 'import json,sys;print([e["state"] for e in json.load(sys.stdin)["environments"] if e["environment"]=="onprem"][0])' 2>/dev/null || echo 없음)
[ "$phase" = Succeeded ] && [ "$state" = OK ] && control=ok || control=fail
check $control "console 네임스페이스에서 조회 API 호출 성공, onprem 상태 OK (pod: $phase, 상태: $state)"
# 대조군(위)이 실패하면 아래 거부는 API가 없어서일 수 있으므로 통과로 세지 않는다
[ "$control" = ok ] && [ "$(call console wrong-token bad)" = Failed ] && r=ok || r=fail
check $r "틀린 토큰은 거부(401)"
[ "$control" = ok ] && [ "$(call stranger "$API_TOKEN" stranger)" = Failed ] && r=ok || r=fail
check $r "라벨이 없는 네임스페이스(stranger)는 올바른 토큰이 있어도 NetworkPolicy가 막음"

echo "== 창 수신 (T36)"
kubectl create namespace loadgen >/dev/null
kubectl label namespace loadgen bravo.observability/window-producer=true >/dev/null
win() { # 창 끝 시각(epoch 초) → 계약을 지킨 빈 SliWindow
  python3 -c 'import json,sys,datetime as d
e=d.datetime.fromtimestamp(int(sys.argv[1]),d.UTC);f=lambda t:t.strftime("%Y-%m-%dT%H:%M:%SZ")
p={"requests":0,"http_5xx":0,"integrity_conflict":0,"p95_ms":None}
print(json.dumps({"kind":"sli_window","schema_version":"1","plan_id":"plan-k8s","source":"loadgen","window":{"start":f(e-d.timedelta(seconds=10)),"end":f(e)},"received_at":f(e),"pools":{"source":p,"target":p},"unattributed":{"timeout":0,"connect":0,"other":0}}))' "$1"
}
post() { # 네임스페이스 토큰 이름 본문 → Pod 결과(2xx면 Succeeded)
  kubectl -n "$1" run "post-$3" --image=busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec \
    --restart=Never -- wget -q -T 5 -O - --header "Authorization: Bearer $2" --header "Content-Type: application/json" \
    --post-data "$4" "http://bravo-window-intake.$NS.svc:8080/v1/observability/windows" >/dev/null
  for _ in $(seq 30); do p=$(kubectl -n "$1" get pod "post-$3" -o jsonpath='{.status.phase}'); case $p in Succeeded|Failed) break;; esac; sleep 2; done
  echo "$p"
}
t0=$(( $(date +%s) - 30 ))
# Independent ten-second windows: a denied request must not consume the
# following valid window. Overlap rejection is tested separately in Python.
W1=$(win "$t0"); W2=$(win $((t0 + 10))); W3=$(win $((t0 + 20)))
phase=$(post loadgen "$WINDOW_TOKEN" ok "$W1")
# 생산자가 넣은 received_at(창 끝)은 버리고 플랫폼이 받은 시각으로 덮어써야 한다
got=$(kubectl -n loadgen logs post-ok 2>/dev/null | W="$W1" python3 -c 'import json,os,sys;d=json.load(sys.stdin);print(d["plan_id"], d["received_at"] != json.loads(os.environ["W"])["received_at"])' 2>/dev/null || echo 없음)
[ "$phase" = Succeeded ] && [ "$got" = "plan-k8s True" ] && control=ok || control=fail
check $control "loadgen 네임스페이스에서 수신 토큰으로 창 저장(201), received_at은 플랫폼 시각 (pod: $phase)"
[ "$control" = ok ] && [ "$(post loadgen "$WINDOW_TOKEN" dup "$W1")" = Failed ] && \
  [ "$(post loadgen "$API_TOKEN" console-token "$W2")" = Failed ] && r=ok || r=fail
check $r "같은 창은 한 번만(409), 콘솔 토큰으로는 보낼 수 없음(401)"
# 막힌 뒤 같은 창을 loadgen이 보내 성공해야, 막힌 쪽이 저장하지 못했음이 확인된다
[ "$control" = ok ] && [ "$(post stranger "$WINDOW_TOKEN" stranger "$W2")" = Failed ] && \
  [ "$(post console "$WINDOW_TOKEN" console "$W3")" = Failed ] && \
  [ "$(post loadgen "$WINDOW_TOKEN" after "$W3")" = Succeeded ] && r=ok || r=fail
check $r "라벨이 없는 네임스페이스와 콘솔 네임스페이스는 올바른 토큰이 있어도 NetworkPolicy가 막음"
dns=$(kubectl -n kube-system get svc kube-dns -o jsonpath='{.spec.clusterIP}')
reach() { kubectl -n "$NS" exec "deploy/$1" -- python -c "import socket;socket.create_connection(('$dns',53),3)" >/dev/null 2>&1; }
reach bravo-observability-api && ! reach bravo-window-intake && r=ok || r=fail
check $r "창 수신은 밖으로 나가지 못함 (대조군: 조회 API는 DNS 53 연결 성공)"

echo "== DB 지표 (CNPG)"
sa=system:serviceaccount:$NS:bravo-collector
[ "$(kubectl auth can-i list pods -n source-dev --as "$sa")" = yes ] && \
  [ "$(kubectl auth can-i get secrets -n source-dev --as "$sa")" = no ] && \
  [ "$(kubectl auth can-i create pods/exec -n source-dev --as "$sa")" = no ] && \
  [ "$(kubectl auth can-i list pods -n default --as "$sa")" = no ] && r=ok || r=fail
check $r "Collector 권한: source-dev Pod 조회만 가능 (Secret · exec · 다른 네임스페이스 거부)"
kubectl -n source-dev wait --for=condition=Ready cluster/source-db --timeout=300s >/dev/null || true
kubectl -n source-dev exec source-db-1 -c postgres -- psql -U postgres -tAc \
  "SELECT pg_create_logical_replication_slot('bravo_t37_slot','pgoutput')" >/dev/null
for _ in $(seq 60); do [ "$(count 'cnpg_pg_replication_slots_pg_wal_lsn_diff{slot_type="logical"}')" -gt 0 ] && break; sleep 3; done
upl=$(q 'cnpg_collector_up{cnpg_cluster="source-db"}' | python3 -c 'import json,sys;r=json.load(sys.stdin)["data"]["result"];print(json.dumps([x["metric"] for x in r]))')
echo "  cnpg_collector_up 라벨: $upl"
echo "$upl" | python3 -c 'import json,sys;ms=json.load(sys.stdin);sys.exit(0 if ms and all(m.get("bravo_env")=="onprem" and m.get("bravo_signal")=="cnpg-scrape" and m.get("pod")=="source-db-1" and m.get("instance_role")=="primary" for m in ms) else 1)' && r=ok || r=fail
check $r "CNPG 인스턴스 지표가 출처(cnpg-scrape) · 환경 · 클러스터 · Pod · 역할 라벨과 함께 도착"
[ "$(count 'cnpg_pg_replication_slots_pg_wal_lsn_diff{slot_name="bravo_t37_slot", slot_type="logical"}')" -gt 0 ] && r=ok || r=fail
check $r "논리 복제 슬롯 지연(바이트) 지표 도착"
echo "  보관 실패 지표 이름: $(q '{__name__=~"cnpg_pg_stat_archiver_failed_count.*"}' | python3 -c 'import json,sys;print(sorted({x["metric"]["__name__"] for x in json.load(sys.stdin)["data"]["result"]}))')"
echo "  DB 수집 up 라벨: $(q 'up{job="bravo-cnpg"}' | python3 -c 'import json,sys;print([x["metric"] for x in json.load(sys.stdin)["data"]["result"]])')"
phase=$(call console "$API_TOKEN" db /v1/observability/environments/onprem/database)
db=$(kubectl -n console logs call-db 2>/dev/null || true)
echo "  조회 API DB 뷰: $db"
echo "$db" | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d["state"]=="OK" and any(s["slot"]=="bravo_t37_slot" and s["type"]=="logical" and s["active"] is False for s in d["replication_slots"]) else 1)' 2>/dev/null && r=ok || r=fail
check $r "조회 API DB 뷰: 상태 OK, 논리 슬롯과 비활성 여부 표시 (pod: $phase)"

echo "== 앱 경로로 DB 지표 사칭 (cnpg_collector_up, service.name=bravo-cnpg, cnpg_cluster 속성)"
kubectl -n app run forger --image="$GEN" --restart=Never -- metrics \
  --otlp-endpoint "bravo-collector.$NS.svc:4317" --otlp-insecure --duration 20s --rate 2 \
  --otlp-metric-name cnpg_collector_up --otlp-attributes 'service.name="bravo-cnpg"' \
  --telemetry-attributes 'cnpg_cluster="forged-db"' >/dev/null
forged() { count 'cnpg_collector_up{bravo_env="onprem"} unless cnpg_collector_up{bravo_signal="cnpg-scrape"}'; }
for _ in $(seq 60); do [ "$(forged)" -gt 0 ] && break; sleep 2; done
n=$(forged)
phase=$(call console "$API_TOKEN" db-forged /v1/observability/environments/onprem/database)
db=$(kubectl -n console logs call-db-forged 2>/dev/null || true)
echo "  사칭 뒤 조회 API DB 뷰: $db"
[ "$n" -gt 0 ] && echo "$db" | python3 -c 'import json,sys;d=json.load(sys.stdin);i=d["instances"];sys.exit(0 if d["state"]=="OK" and i and all(x["cluster"]=="source-db" and x["pod"] for x in i) else 1)' 2>/dev/null && r=ok || r=fail
check $r "앱이 보낸 cnpg_collector_up(${n}개, 출처 라벨 없음)은 DB 뷰에 들어오지 않음: 상태 OK, 인스턴스는 source-db만 (pod: $phase)"

if [ "${BRAVO_K8S_CAPACITY:-0}" = 1 ]; then
  echo "== 자원 실측: 평상(60초)"
  python3 stats.py "$NAME" 60 5 collector prometheus api intake
  echo "== 자원 실측: 부하(앱 지표 약 2000건/초 = 데모 예상의 약 10배, 창 10건/초 = 100배, 180초)"
  kubectl -n loadgen run winload --image="$API_IMAGE" --restart=Never \
    --env=BRAVO_WINDOW_TOKEN="$WINDOW_TOKEN" --command -- python -c '
import collections, datetime as d, os, time, httpx
from bravo_observability import Record, build_window
recs = [Record("aws", 200, 100.0)] * 1800 + [Record("onprem", 200, 120.0)] * 200
c = httpx.Client(base_url="http://bravo-window-intake.bravo-observability.svc:8080", headers={"Authorization": "Bearer " + os.environ["BRAVO_WINDOW_TOKEN"]})
f = lambda t: t.isoformat().replace("+00:00", "Z")
stop, results = time.time() + 180, collections.Counter()
previous_end = d.datetime.now(d.UTC) - d.timedelta(seconds=1.1)
while time.time() < stop:
    e = d.datetime.now(d.UTC) - d.timedelta(seconds=1)
    w = build_window(recs, plan_id="plan-k8s", start=f(previous_end), end=f(e), pools={"aws": "source", "onprem": "target"})
    try:
        results[c.post("/v1/observability/windows", json=w).status_code] += 1
    except httpx.HTTPError as err:
        results[type(err).__name__] += 1
    previous_end = e
    time.sleep(0.1)
print(dict(results))' >/dev/null
  kubectl -n app run load --image="$GEN" --restart=Never -- metrics \
    --otlp-endpoint "bravo-collector.$NS.svc:4317" --otlp-insecure --duration 180s --workers 20 --rate 100 \
    --otlp-metric-name bravo_load --otlp-attributes 'service.name="demo-chat"' >/dev/null
  python3 stats.py "$NAME" 180 5 collector prometheus api intake
  kubectl -n loadgen wait --for=jsonpath='{.status.phase}'=Succeeded pod/winload --timeout=60s >/dev/null || true
  echo "  창 전송 결과(응답 코드별): $(kubectl -n loadgen logs winload 2>/dev/null | tail -1 || echo 없음)"
  echo "  창 수신 재시작: $(kubectl -n "$NS" get pod -l app=bravo-window-intake -o jsonpath='{.items[0].status.containerStatuses[0].restartCount} {.items[0].status.containerStatuses[0].lastState.terminated.reason}')"
  kubectl -n "$NS" get events --field-selector involvedObject.kind=Pod -o custom-columns=R:.reason,O:.involvedObject.name,M:.message --no-headers 2>/dev/null | grep -E "Unhealthy|BackOff|Killing" | sed 's/^/  이벤트: /' || true
  echo "  저장소 시계열 수: $(q 'prometheus_tsdb_head_series' | python3 -c 'import json,sys;r=json.load(sys.stdin)["data"]["result"];print(r[0]["value"][1] if r else "자기 지표 미수집")')"
fi

# 미리 기동·검증한 같은 클라이언트로 전후를 비교한다. 새 Pod의 첫 접속 지연과
# Collector 중단에 따른 API 상태 변화를 섞지 않는다. 실시간 가용성 시험은 별도다.
kubectl -n console run observer --image=busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec --restart=Never -- sleep 600 >/dev/null
kubectl -n console wait pod/observer --for=condition=Ready --timeout=60s >/dev/null
observe_db() { kubectl -n console exec observer -- wget -q -T 5 -O - --header "Authorization: Bearer $API_TOKEN" "http://bravo-observability-api.$NS.svc:8080/v1/observability/environments/onprem/database"; }
observe_db | python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin)["state"]=="OK" else 1)'

echo "== Collector 중지 → 알림"
for _ in $(seq 60); do [ "$(count 'up{bravo_signal="collector-self", bravo_env="onprem"}')" -gt 0 ] && break; sleep 2; done
kubectl -n "$NS" scale deploy/bravo-collector --replicas=0 >/dev/null
firing=""
for _ in $(seq 60); do
  firing=$(as "$PROM/api/v1/alerts" 2>/dev/null | python3 -c 'import json,sys;print(",".join(sorted(a["labels"].get("bravo_env","") for a in json.load(sys.stdin)["data"]["alerts"] if a["labels"]["alertname"]=="BravoEnvUnobservable" and a["state"]=="firing")))' 2>/dev/null || true)
  case ",$firing," in *,onprem,*) break;; esac
  sleep 2
done
case ",$firing," in *,onprem,*) r=ok;; *) r=fail;; esac
check $r "Collector를 0개로 줄인 뒤 BravoEnvUnobservable이 onprem에 대해 firing (firing 환경: ${firing:-없음})"

db=$(observe_db)
echo "$db" | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d["state"]=="UNOBSERVABLE" and not d["instances"] and not d["replication_slots"] else 1)' && r=ok || r=fail
check $r "기존 CNPG 정상 표본이 남아도 Collector 중단 뒤 DB 뷰는 확인 불가"

echo
echo "실패 ${fails}건"
exit $((fails > 0))
