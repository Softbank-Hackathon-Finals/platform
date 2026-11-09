#!/usr/bin/env bash
# ADR-0016 목적지 감사 저장소 k3s 시제품 시험. Docker 안 k3s v1.35.9(목적지 RKE2 v1.35.9와 같은 쿠버네티스)에
# templates/target-audit-store를 그대로 적용한다. 클라우드 자격 · 공개 포트를 쓰지 않고, 끝나면 클러스터와 키를 지운다.
#  1. 저장소(T30과 같은 SeaweedFS 인자 + S3 TLS)와 전달기가 restricted 네임스페이스에서 뜬다
#  2. 관리 역할 Pod가 Object Lock 버킷 · COMPLIANCE 보존 · 버킷 정책을 만든다(-s3.iam=false에서도 정책 적용)
#  3. source 역할 Pod의 OTLP 감사 기록이 전달기 → HTTPS → 저장소에 OTLP JSON 객체로 남는다
#  4. 검색 역할 Pod는 검색 키만으로 버전 목록을 끝까지 읽고 변조 흔적이 없음을 확인한다. PUT은 거부된다
#  5. 쓰기 · 검색 · 관리 권한과 덮어쓰기 · 삭제 표시 함정이 Docker 실측과 같다
#  6. 역할 라벨 없는 Pod(같은 네임스페이스)와 다른 네임스페이스 Pod는 저장소 8333에 닿지 않는다
set -euo pipefail
cd "$(dirname "$0")"

K3S=rancher/k3s:v1.35.9-k3s1@sha256:ec9868c6a38d4e8c1869832fb5fd1eb8473c39794a0b44d2b952e7ba911951bc
PYTHON=python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a
BUSYBOX=busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec
BOTO3=boto3==1.40.70
NAME="bravo-audit-k3s-$(openssl rand -hex 6)"
PROBE_IMAGE="docker.io/library/${NAME}-probe:dev"
NS=bravo-audit
WORK=$(mktemp -d)
KUBECONFIG="$WORK/kubeconfig"
export KUBECONFIG
CONTAINER_ID=""
cleanup() {
  if [ -n "$CONTAINER_ID" ]; then docker rm -f "$CONTAINER_ID" >/dev/null 2>&1 || true; fi
  docker image rm -f "$PROBE_IMAGE" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
fails=0
check() { if [ "$1" = ok ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails + 1)); fi; }
restricted='securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}'
podsec='securityContext: {runAsNonRoot: true, runAsUser: 65534, runAsGroup: 65534, seccompProfile: {type: RuntimeDefault}}'

# 실행마다 새 CA · 서버 인증서와 세 identity 키. 값은 출력하지 않고 WORK에만 둔다.
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=Audit store test CA" \
  -keyout "$WORK/ca.key" -out "$WORK/ca.crt" 2>/dev/null
printf '[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=audit-store\n[ext]\nsubjectAltName=DNS:audit-store,DNS:audit-store.%s.svc,DNS:audit-store.%s.svc.cluster.local\nextendedKeyUsage=serverAuth\n' "$NS" "$NS" > "$WORK/openssl.cnf"
openssl req -new -newkey rsa:2048 -nodes -config "$WORK/openssl.cnf" -keyout "$WORK/tls.key" -out "$WORK/tls.csr" 2>/dev/null
openssl x509 -req -in "$WORK/tls.csr" -CA "$WORK/ca.crt" -CAkey "$WORK/ca.key" -CAcreateserial -days 1 \
  -extfile "$WORK/openssl.cnf" -extensions ext -out "$WORK/tls.crt" 2>/dev/null
python3 - "$WORK" <<'PY'
import json, secrets, sys
from pathlib import Path
work = Path(sys.argv[1])
keys = {p: (secrets.token_hex(10).upper(), secrets.token_hex(20)) for p in ("ADMIN", "WRITER", "READER")}
actions = {"ADMIN": [f"{a}:bravo-audit" for a in ("Admin", "Read", "Write", "List", "Tagging")],
           "WRITER": ["Write:bravo-audit"], "READER": ["Read:bravo-audit", "List:bravo-audit"]}
names = {"ADMIN": "admin", "WRITER": "audit-writer", "READER": "audit-reader"}
(work / "s3.json").write_text(json.dumps({"identities": [
    {"name": names[p], "credentials": [{"accessKey": ak, "secretKey": sk}], "actions": actions[p]} for p, (ak, sk) in keys.items()]}))
for p, (ak, sk) in keys.items():
    (work / f"{p.lower()}.env").write_text(f"{p}_AK={ak}\n{p}_SK={sk}\n")
ak, sk = keys["WRITER"]
(work / "writer-aws.env").write_text(f"AWS_ACCESS_KEY_ID={ak}\nAWS_SECRET_ACCESS_KEY={sk}\n")
PY

echo "== k3s 시작"
CONTAINER_ID=$(docker run -d --privileged --name "$NAME" -p 127.0.0.1::6443 --tmpfs /run --tmpfs /var/run \
  "$K3S" server --disable=traefik --disable=metrics-server --disable=servicelb --tls-san=127.0.0.1)
for _ in $(seq 60); do docker exec "$NAME" test -f /etc/rancher/k3s/k3s.yaml 2>/dev/null && break; sleep 2; done
docker exec "$NAME" cat /etc/rancher/k3s/k3s.yaml | sed "s#https://127.0.0.1:6443#https://$(docker port "$NAME" 6443)#" > "$KUBECONFIG"
for _ in $(seq 60); do kubectl get nodes 2>/dev/null | grep -q " Ready" && break; sleep 2; done
kubectl wait --for=condition=Ready node --all --timeout=120s >/dev/null
# 검사 이미지(boto3)는 로컬에서 만들어 k3s에 넣는다. 저장소 · 전달기 이미지는 digest로 받는다.
printf 'FROM %s\nRUN pip install --no-cache-dir %s\nENV PYTHONDONTWRITEBYTECODE=1\n' "$PYTHON" "$BOTO3" | docker build -q -t "$PROBE_IMAGE" - >/dev/null
docker save "$PROBE_IMAGE" | docker exec -i "$NAME" ctr -n k8s.io images import - >/dev/null

echo "== 배포"
kubectl create namespace "$NS" --save-config >/dev/null
kubectl -n "$NS" create secret generic audit-store-auth --from-file="$WORK/s3.json" >/dev/null
kubectl -n "$NS" create secret generic audit-store-tls --from-file="$WORK/tls.crt" --from-file="$WORK/tls.key" >/dev/null
kubectl -n "$NS" create secret generic audit-ca --from-file="$WORK/ca.crt" >/dev/null
kubectl -n "$NS" create secret generic audit-writer --from-env-file="$WORK/writer-aws.env" >/dev/null
for who in admin writer reader; do
  kubectl -n "$NS" create secret generic "probe-$who" --from-env-file="$WORK/$who.env" >/dev/null
done
kubectl -n "$NS" create configmap audit-probe --from-file=verify_target_audit_store.py >/dev/null
kubectl apply -k ../../templates/target-audit-store >/dev/null
r=ok
kubectl -n "$NS" rollout status deploy/audit-store --timeout=600s >/dev/null || r=fail
kubectl -n "$NS" rollout status deploy/audit-forwarder --timeout=240s >/dev/null || r=fail
check $r "restricted 네임스페이스에서 저장소(SeaweedFS 4.52, S3 TLS) · 전달기가 준비됨"

# Pod를 띄워 끝날 때까지 기다리고 마지막 줄(JSON)을 돌려준다. 역할 라벨이 NetworkPolicy 허용을 정한다.
probe() { # 이름 역할 단계 secret...
  local name=$1 role=$2 phase=$3; shift 3
  local env=""
  for s in "$@"; do env="$env, {secretRef: {name: $s}}"; done
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: {name: $name, namespace: $NS, labels: {bravo.audit/role: $role}}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  $podsec
  containers:
    - name: probe
      image: $PROBE_IMAGE
      imagePullPolicy: Never
      command: ["python", "/probe/verify_target_audit_store.py", "--probe", "$phase"]
      env: [{name: AWS_CA_BUNDLE, value: /etc/audit-ca/ca.crt}, {name: AUDIT_STORE_ENDPOINT, value: "https://audit-store:8333"}]
      envFrom: [${env#, }]
      $restricted
      volumeMounts: [{name: probe, mountPath: /probe, readOnly: true}, {name: ca, mountPath: /etc/audit-ca, readOnly: true}]
  volumes: [{name: probe, configMap: {name: audit-probe}}, {name: ca, secret: {secretName: audit-ca}}]
EOF
  for _ in $(seq 60); do p=$(kubectl -n "$NS" get pod "$name" -o jsonpath='{.status.phase}'); case $p in Succeeded|Failed) break;; esac; sleep 2; done
  kubectl -n "$NS" logs "$name" 2>/dev/null | tail -1
}
# 저장소 S3 연결을 시험한다(nc). 결과 phase만 돌려준다.
reach() { # 네임스페이스 이름 역할(없으면 빈 값)
  local meta="{name: $2, namespace: $1}"
  [ -n "$3" ] && meta="{name: $2, namespace: $1, labels: {bravo.audit/role: $3}}"
  kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: $meta
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  $podsec
  containers:
    - name: nc
      image: $BUSYBOX
      command: ["nc", "-z", "-w", "5", "audit-store.$NS.svc", "8333"]
      $restricted
EOF
  for _ in $(seq 30); do p=$(kubectl -n "$1" get pod "$2" -o jsonpath='{.status.phase}'); case $p in Succeeded|Failed) break;; esac; sleep 2; done
  echo "$p"
}
field() { python3 -c 'import json,sys;d=json.loads(sys.argv[1]);print(d.get(sys.argv[2]))' "$1" "$2" 2>/dev/null || echo 없음; }

echo "== 버킷 · 보존 · 정책 (관리 역할)"
setup=$(probe audit-bootstrap bootstrap setup probe-admin)
[ "$(field "$setup" admin.create_bucket_object_lock)" = ok ] && [ "$(field "$setup" admin.put_default_retention)" = ok ] && \
  [ "$(field "$setup" versioning)" = Enabled ] && [ "$(field "$setup" admin.put_bucket_policy)" = ok ] && r=ok || r=fail
check $r "Object Lock 버킷 · COMPLIANCE 기본 보존 · 버전 관리 · 버킷 정책 생성 (결과: $setup)"

echo "== 전달기 → 저장소"
body='{"resourceLogs":[{"resource":{"attributes":[{"key":"bravo.audit.source","value":{"stringValue":"cnpg"}}]},"scopeLogs":[{"logRecords":[{"timeUnixNano":"'"$(python3 -c 'import time;print(time.time_ns())')"'","body":{"stringValue":"synthetic audit record"}}]}]}]}'
kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: {name: audit-source, namespace: $NS, labels: {bravo.audit/role: source}}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  $podsec
  containers:
    - name: post
      image: $BUSYBOX
      command: ["sh", "-c", "for i in 1 2 3 4 5 6 7 8 9 10; do wget -q -T 5 -O /dev/null --header 'Content-Type: application/json' --post-data \"\$BODY\" http://audit-forwarder:4318/v1/logs && exit 0; sleep 3; done; exit 1"]
      env: [{name: BODY, value: '$body'}]
      $restricted
EOF
for _ in $(seq 60); do p=$(kubectl -n "$NS" get pod audit-source -o jsonpath='{.status.phase}'); case $p in Succeeded|Failed) break;; esac; sleep 2; done
[ "$p" = Succeeded ] && r=ok || r=fail
check $r "source 역할 Pod의 OTLP 감사 기록을 전달기가 받음 (pod: $p)"
search=""
for _ in $(seq 10); do
  kubectl -n "$NS" delete pod audit-search --ignore-not-found --wait=true >/dev/null
  search=$(probe audit-search searcher search probe-reader)
  [ "$(field "$search" search.objects)" != 0 ] && [ "$(field "$search" search.objects)" != 없음 ] && break
  sleep 5
done
[ "$(field "$search" search.complete)" = True ] && [ "$(field "$search" search.tamper_evidence)" = False ] && \
  [ "$(field "$search" search.objects)" -ge 1 ] 2>/dev/null && [ "$(field "$search" search.otlp_records)" -ge 1 ] 2>/dev/null && \
  [ "$(field "$search" search.put_object)" = denied:AccessDenied ] && r=ok || r=fail
check $r "검색 역할은 검색 키만으로 끝까지 읽고 변조 흔적 없음, PUT 거부 (결과: $search)"

echo "== 권한 · 함정 (-s3.iam=false, S3 TLS)"
rights=$(probe audit-rights bootstrap rights probe-admin probe-writer probe-reader)
r=ok
for k in writer.delete_object writer.delete_version writer.suspend_versioning writer.delete_bucket_policy writer.get_object writer.list \
         reader.put_object reader.delete_object admin.delete_locked_version anonymous.list; do
  [ "$(field "$rights" "$k")" = denied:AccessDenied ] || r=fail
done
for k in writer.put_if_none_match reader.list reader.list_versions reader.get_version; do [ "$(field "$rights" "$k")" = ok ] || r=fail; done
[ "$(field "$rights" writer.put_existing_key_if_none_match)" = denied:PreconditionFailed ] || r=fail
for k in hazard.unconditional_overwrite_adds_version hazard.delete_marker_hides_from_plain_list original_version_intact last_modified_bounds_put; do
  [ "$(field "$rights" "$k")" = True ] || r=fail
done
check $r "쓰기 전용 · 검색 전용 · 보존 잠금 · 익명 거부와 두 함정이 Docker 실측과 같음"

echo "== 네트워크"
kubectl create namespace stranger >/dev/null
unlabeled=$(reach "$NS" probe-unlabeled "")
stranger=$(reach stranger probe-stranger "")
allowed=$(reach "$NS" probe-searcher searcher)
[ "$allowed" = Succeeded ] && [ "$unlabeled" = Failed ] && [ "$stranger" = Failed ] && r=ok || r=fail
check $r "역할 라벨 없는 Pod · 다른 네임스페이스 Pod는 저장소 8333에 닿지 않음 (searcher: $allowed, 라벨 없음: $unlabeled, stranger: $stranger)"

echo
echo "실패 ${fails}건"
exit $((fails > 0))
