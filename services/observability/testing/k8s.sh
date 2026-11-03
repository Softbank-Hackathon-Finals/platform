#!/usr/bin/env bash
# T37 쿠버네티스 배포 시험. Docker 안에 k3s(T7 RKE2와 같은 쿠버네티스 1.35)를 띄우고 deploy/collector · deploy/platform을 그대로 적용한다.
# 한 클러스터가 플랫폼과 목적지(onprem) 역할을 함께 한다. 끝나면 클러스터와 인증서를 지운다.
#  1. 두 매니페스트가 Pod 보안 표준 restricted 네임스페이스에서 뜬다 (비루트 · 읽기 전용 루트 · 권한 제거)
#  2. 라벨(bravo.observability/send=true)을 붙인 네임스페이스의 앱 지표만 Collector를 거쳐 mTLS로 저장소에 도착한다
#  3. 라벨이 없는 네임스페이스는 NetworkPolicy가 막는다 (Collector로 보내기, 지표 저장소 연결 모두)
#  4. Collector를 0개로 줄이면 BravoEnvUnobservable 알림이 onprem에 대해 뜬다
#  5. 콘솔용 조회 API가 restricted로 뜨고, platform-client 라벨 네임스페이스에서만 호출된다(토큰 필수)
set -euo pipefail
cd "$(dirname "$0")"

K3S=rancher/k3s:v1.35.9-k3s1@sha256:ec9868c6a38d4e8c1869832fb5fd1eb8473c39794a0b44d2b952e7ba911951bc
GEN=ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:v0.164.0@sha256:b536c7220c5ea587f04dff6c5e6eecb48e40b5b58ce70690b15734aae98d8321
NAME=bravo-obs-k3s
NS=bravo-observability
CERT_DIR=$(mktemp -d)
KUBECONFIG="$CERT_DIR/kubeconfig"
API_TOKEN=$(openssl rand -hex 24)
export CERT_DIR KUBECONFIG
PF=""
cleanup() { [ -n "$PF" ] && { kill "$PF" 2>/dev/null; wait "$PF" 2>/dev/null; }; docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$CERT_DIR"; }
trap cleanup EXIT
fails=0
check() { if [ "$1" = ok ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails + 1)); fi; }

# shellcheck source=certs.sh
source ./certs.sh
ca bravo-ca
leaf prometheus bravo-ca "DNS:bravo-prometheus.$NS.svc,DNS:bravo-prometheus.$NS.svc.cluster.local,DNS:localhost,IP:127.0.0.1"
leaf collector-onprem bravo-ca "DNS:collector-onprem"
leaf console-api bravo-ca "DNS:console-api"

echo "== k3s 시작"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --privileged --name "$NAME" -p 127.0.0.1::6443 --tmpfs /run --tmpfs /var/run \
  "$K3S" server --disable=traefik --disable=metrics-server --disable=servicelb --tls-san=127.0.0.1 >/dev/null
for _ in $(seq 60); do docker exec "$NAME" test -f /etc/rancher/k3s/k3s.yaml 2>/dev/null && break; sleep 2; done
docker exec "$NAME" cat /etc/rancher/k3s/k3s.yaml | sed "s#https://127.0.0.1:6443#https://$(docker port "$NAME" 6443)#" > "$KUBECONFIG"
for _ in $(seq 60); do kubectl get nodes 2>/dev/null | grep -q " Ready" && break; sleep 2; done
kubectl wait --for=condition=Ready node --all --timeout=120s >/dev/null
# 조회 API 이미지를 로컬에서 빌드해 k3s에 넣는다(레지스트리를 쓰지 않음)
docker build -q -t bravo-observability-api:dev .. >/dev/null
docker save bravo-observability-api:dev | docker exec -i "$NAME" ctr -n k8s.io images import - >/dev/null

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
kubectl -n "$NS" create configmap bravo-collector-env --from-literal=BRAVO_ENV=onprem \
  --from-literal=PLATFORM_OTLP_ENDPOINT="https://bravo-prometheus.$NS.svc:9090/api/v1/otlp" >/dev/null
kubectl apply -k k8s-overlay >/dev/null
kubectl apply -k ../deploy/collector >/dev/null
r=ok
kubectl -n "$NS" rollout status deploy/bravo-prometheus --timeout=240s >/dev/null || r=fail
kubectl -n "$NS" rollout status deploy/bravo-collector --timeout=240s >/dev/null || r=fail
kubectl -n "$NS" rollout status deploy/bravo-observability-api --timeout=240s >/dev/null || r=fail
check $r "restricted 네임스페이스에서 지표 저장소 · Collector · 조회 API가 준비됨"

kubectl -n "$NS" port-forward svc/bravo-prometheus 19090:9090 >/dev/null 2>&1 &
PF=$!
PROM=https://127.0.0.1:19090
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
call() { # 네임스페이스 토큰
  kubectl -n "$1" run "call-$3" --image=busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec \
    --restart=Never -- wget -q -T 5 -O - --header "Authorization: Bearer $2" "http://bravo-observability-api.$NS.svc:8080/v1/observability/environments" >/dev/null
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

echo
echo "실패 ${fails}건"
exit $((fails > 0))
