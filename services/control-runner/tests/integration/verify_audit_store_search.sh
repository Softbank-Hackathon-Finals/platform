#!/usr/bin/env bash
# T34 검색 어댑터(audit_store.py)를 실제 SeaweedFS 4.52 + Collector 0.164.0(awss3)이 쓴 객체로 시험한다.
# 내부 네트워크 · 일회용 키 · 공개 포트 없음. 끝나면 컨테이너 · 이미지 · 키를 지운다.
#  1. 정상: 등록 출처의 표식 접속 기록 1건 → present
#  2. 같은 기록을 짧은 COMPLIANCE 기한으로 쓴 사본, 기한이 지난 뒤 → UNKNOWN(보존 중인 버전만 증거)
#  3. 관리자가 같은 키를 덮어씀 → 변조 흔적(UNKNOWN)
#  4. 관리자가 삭제 표시를 남김 → 변조 흔적(UNKNOWN)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../../.." && pwd)
SW=docker.io/chrislusf/seaweedfs:4.52@sha256:a54ba35b44895a449f5cdd415999062201b17b9835ec0ffc19ea605b72ffa3d1
COL=otel/opentelemetry-collector-contrib:0.164.0@sha256:340c9ca6a36617084c6661bdc4f0fe92b7cda5a805d61673ec8db375fddc522b
PY=python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a
W=$(mktemp -d); N="ad$(openssl rand -hex 3)"
AK=$(openssl rand -hex 10); SK=$(openssl rand -hex 20); WK=$(openssl rand -hex 10); WS=$(openssl rand -hex 20); RK=$(openssl rand -hex 10); RS=$(openssl rand -hex 20)
cleanup() { docker rm -f "$N-s3" "$N-col" >/dev/null 2>&1 || true; docker network rm "$N" >/dev/null 2>&1 || true; docker image rm -f "$N-cr" >/dev/null 2>&1 || true; rm -rf "$W"; }
trap cleanup EXIT
python3 - "$W" "$AK" "$SK" "$WK" "$WS" "$RK" "$RS" <<'PY'
import json, sys
w, ak, sk, wk, ws, rk, rs = sys.argv[1:]
json.dump({"identities": [
  {"name": "admin", "credentials": [{"accessKey": ak, "secretKey": sk}], "actions": [f"{a}:bravo-audit" for a in ("Admin", "Read", "Write", "List")]},
  {"name": "audit-writer", "credentials": [{"accessKey": wk, "secretKey": ws}], "actions": ["Write:bravo-audit"]},
  {"name": "audit-reader", "credentials": [{"accessKey": rk, "secretKey": rs}], "actions": ["Read:bravo-audit", "List:bravo-audit"]}]}, open(f"{w}/s3.json", "w"))
PY
cat > "$W/col.yaml" <<YAML
receivers: {otlp: {protocols: {http: {endpoint: 0.0.0.0:4318}}}}
exporters:
  awss3:
    s3uploader: {region: us-east-1, s3_bucket: bravo-audit, s3_prefix: audit/onprem, s3_partition_format: '%Y/%m/%d/%H/%M', s3_partition_timezone: UTC, endpoint: 'http://s3:8333', s3_force_path_style: true, unique_key_func_name: uuidv7}
    marshaler: otlp_json
service: {pipelines: {logs: {receivers: [otlp], exporters: [awss3]}}}
YAML
cp "$HERE/audit_store_search_probe.py" "$W/probe.py"
chmod -R a+rwX "$W"
# control-runner 패키지(이 브랜치)를 넣은 실행 이미지
printf 'FROM %s\nRUN pip install --no-cache-dir uv==0.13.6\nCOPY services/control-runner /cr\nWORKDIR /cr\nRUN uv sync --locked --no-dev --all-extras\nENV PATH=/cr/.venv/bin:$PATH\n' "$PY" > "$W/Dockerfile"
docker build -q -t "$N-cr" -f "$W/Dockerfile" "$REPO" >/dev/null
docker network create --internal "$N" >/dev/null
docker run -d --platform linux/amd64 --name "$N-s3" --network "$N" --network-alias s3 -v "$W/s3.json:/etc/s3.json:ro" "$SW" server -dir=/data -s3 -s3.config=/etc/s3.json -s3.port.iceberg=0 -s3.port.lance=0 >/dev/null
run() { docker run --rm -i --network "$N" -e AK="$AK" -e SK="$SK" -e RK="$RK" -e RS="$RS" -v "$W:/w" "$N-cr" python "$@"; }
for _ in $(seq 30); do run -c "import botocore.session,os;from botocore.config import Config;c=botocore.session.get_session().create_client('s3',endpoint_url='http://s3:8333',region_name='us-east-1',aws_access_key_id=os.environ['AK'],aws_secret_access_key=os.environ['SK'],config=Config(s3={'addressing_style':'path'}));c.create_bucket(Bucket='bravo-audit',ObjectLockEnabledForBucket=True);c.put_object_lock_configuration(Bucket='bravo-audit',ObjectLockConfiguration={'ObjectLockEnabled':'Enabled','Rule':{'DefaultRetention':{'Mode':'COMPLIANCE','Days':1}}})" >/dev/null 2>&1 && break; sleep 2; done
docker run -d --name "$N-col" --network "$N" -e AWS_ACCESS_KEY_ID="$WK" -e AWS_SECRET_ACCESS_KEY="$WS" -v "$W/col.yaml:/etc/col.yaml:ro" "$COL" --config=/etc/col.yaml >/dev/null
sleep 4
# 등록된 출처 속성과 표식 접속 기록 하나를 OTLP로 보낸다
run - <<PY
import json, secrets, time, urllib.request
from datetime import UTC, datetime, timedelta
from bravo_controls.otlp_audit import AuditOrigin
o = AuditOrigin("target", "sha256:" + "c" * 64, "22222222-2222-4222-8222-222222222222", "11111111-1111-4111-8111-111111111111", "target-dev", "target-db-1")
marker = "bravo_audit_" + secrets.token_hex(16)
start = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=5)
v = lambda x: {"kvlistValue": {"values": [{"key": k, "value": v(y)} for k, y in x.items()]}} if isinstance(x, dict) else {"stringValue": x}
body = {"logger": "postgres", "record": {"user_name": "postgres", "database_name": "chat", "application_name": marker, "error_severity": "LOG",
        "message": "connection authorized: user=postgres database=chat application_name=" + marker}}
doc = {"resourceLogs": [{"resource": {"attributes": [{"key": k, "value": v(x)} for k, x in o.attributes("cnpg").items()]},
       "scopeLogs": [{"scope": {}, "logRecords": [{"timeUnixNano": str(int((start + timedelta(seconds=2)).timestamp()) * 10**9), "body": v(body)}]}]}]}
urllib.request.urlopen(urllib.request.Request("http://$N-col:4318/v1/logs", data=json.dumps(doc).encode(), headers={"Content-Type": "application/json"}), timeout=5)
json.dump({"marker": marker, "start": start.isoformat()}, open("/w/state.json", "w"))
PY
sleep 8
fails=0
expect() { # 이름 단계 기대(파이썬 식)
  local got; got=$(run /w/probe.py "$2" | tail -1)
  if python3 -c "import json,sys; r=json.loads(sys.argv[1]); sys.exit(0 if ($3) else 1)" "$got"; then echo "  ok   $1 $got"; else echo "  FAIL $1 $got"; fails=$((fails + 1)); fi
}
expect "정상: 표식 접속 기록 → present" normal 'r.get("observed") == "present" and r.get("events") == 1'
expect "보존 기한이 지난 사본 → UNKNOWN" expired-retention '"retention missing or expired" in r.get("error", "")'
expect "관리자 덮어쓰기 → 변조 흔적" tamper-overwrite '"several versions" in r.get("error", "")'
expect "관리자 삭제 표시 → 변조 흔적" tamper-delete '"delete marker" in r.get("error", "")'
echo "실패 ${fails}건"
exit $((fails > 0))
