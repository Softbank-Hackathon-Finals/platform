#!/usr/bin/env bash
# render.py의 신원 · 버킷 정책을 실제 SeaweedFS 4.52(S3 TLS)에 넣고 verify.py로 잰다. 일회용 키 · 내부 네트워크 ·
# 공개 포트 없음. 끝나면 컨테이너 · 이미지 · 키를 지운다.
#  1. 렌더링한 정책: verify.py PASS(쓰기 신원은 넣기 · 읽기 · 목록만, 읽기 신원은 읽기 · 목록만, 삭제 · 보호 변경 거부)
#  2. 삭제 거부 문을 뺀 정책: verify.py FAIL(쓰기 신원이 지울 수 있음을 잡는다)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
SW=docker.io/chrislusf/seaweedfs:4.52@sha256:a54ba35b44895a449f5cdd415999062201b17b9835ec0ffc19ea605b72ffa3d1
PY=python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a
W=$(mktemp -d); N="t35tb$(openssl rand -hex 3)"
cleanup() { docker rm -f "$N-s3" >/dev/null 2>&1 || true; docker network rm "$N" >/dev/null 2>&1 || true; docker image rm -f "$N-cr" >/dev/null 2>&1 || true; rm -rf "$W"; }
trap cleanup EXIT

openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=t35 backup test CA" -keyout "$W/ca.key" -out "$W/ca.crt" 2>/dev/null
printf '[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=seaweedfs\n[ext]\nsubjectAltName=DNS:seaweedfs\nextendedKeyUsage=serverAuth\n' > "$W/tls.cnf"
openssl req -new -newkey rsa:2048 -nodes -config "$W/tls.cnf" -keyout "$W/tls.key" -out "$W/tls.csr" 2>/dev/null
openssl x509 -req -in "$W/tls.csr" -CA "$W/ca.crt" -CAkey "$W/ca.key" -CAcreateserial -days 1 -extfile "$W/tls.cnf" -extensions ext -out "$W/tls.crt" 2>/dev/null
rm "$W/ca.key"

printf 'FROM %s\nRUN pip install --no-cache-dir uv==0.13.6\nCOPY services/control-runner /cr\nCOPY infra/envs/target-backup /tb\nWORKDIR /cr\nRUN uv sync --locked --no-dev --all-extras\nENV PATH=/cr/.venv/bin:$PATH\nWORKDIR /tb\n' "$PY" > "$W/Dockerfile"
docker build -q -t "$N-cr" -f "$W/Dockerfile" "$REPO" >/dev/null
docker run --rm "$N-cr" python render.py identities > "$W/identities.json"
docker run --rm "$N-cr" python render.py bucket-policy > "$W/policy.json"
python3 - "$W" <<'PY'
import json, secrets, sys
w = sys.argv[1]
ids = json.load(open(f"{w}/identities.json"))
names = {"t35-backup-writer": "WRITER", "t35-backup-reader": "READER"}
keys = {who: (secrets.token_hex(10).upper(), secrets.token_hex(20)) for who in ("ADMIN", "WRITER", "READER")}
cfg = [{"name": i["name"], "credentials": [{"accessKey": keys[names[i["name"]]][0], "secretKey": keys[names[i["name"]]][1]}],
        "actions": i["actions"]} for i in ids]
cfg.append({"name": "admin", "credentials": [{"accessKey": keys["ADMIN"][0], "secretKey": keys["ADMIN"][1]}],
            "actions": [f"{a}:bravo-backup" for a in ("Admin", "Read", "Write", "List")]})
json.dump({"identities": cfg}, open(f"{w}/s3.json", "w"))
open(f"{w}/keys.env", "w").write("".join(f"T35_{who}_AK={k[0]}\nT35_{who}_SK={k[1]}\n" for who, k in keys.items()))
weak = json.load(open(f"{w}/policy.json"))
weak["Statement"] = [s for s in weak["Statement"] if s["Sid"] != "BackupIdentitiesCannotDelete"]
json.dump(weak, open(f"{w}/weak.json", "w"))
PY
chmod -R a+rX "$W"
docker network create --internal "$N" >/dev/null
docker run -d --platform linux/amd64 --name "$N-s3" --network "$N" --network-alias seaweedfs \
  -v "$W/s3.json:/etc/s3.json:ro" -v "$W/tls.crt:/tls/tls.crt:ro" -v "$W/tls.key:/tls/tls.key:ro" "$SW" \
  server -dir=/data -s3 -s3.config=/etc/s3.json -s3.iam=false -s3.cert.file=/tls/tls.crt -s3.key.file=/tls/tls.key \
  -s3.port.iceberg=0 -s3.port.lance=0 >/dev/null
run() { docker run --rm -i --network "$N" --env-file "$W/keys.env" -v "$W:/w:ro" "$N-cr" "$@"; }
# 관리자(버킷 소유자)가 버킷 · 버전 관리 · 정책을 만든다. 정책 파일을 인자로 받는다.
setup='import json, os, sys, time
import botocore.session
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError
c = botocore.session.get_session().create_client("s3", region_name="us-east-1", endpoint_url="https://seaweedfs:8333", verify="/w/ca.crt",
    aws_access_key_id=os.environ["T35_ADMIN_AK"], aws_secret_access_key=os.environ["T35_ADMIN_SK"], config=Config(s3={"addressing_style": "path"}))
for _ in range(30):
    try:
        c.head_bucket(Bucket="bravo-backup") if "--existing" in sys.argv else c.create_bucket(Bucket="bravo-backup"); break
    except EndpointConnectionError:
        time.sleep(2)
c.put_bucket_versioning(Bucket="bravo-backup", VersioningConfiguration={"Status": "Enabled"})
c.put_bucket_policy(Bucket="bravo-backup", Policy=open(sys.argv[1]).read())'
fails=0
expect() { # 이름 기대상태
  local got; got=$(run python verify.py --endpoint https://seaweedfs:8333 --ca /w/ca.crt | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])') || true
  if [ "$got" = "$2" ]; then echo "  ok   $1 (status $got)"; else echo "  FAIL $1 (status ${got:-없음}, 기대 $2)"; fails=$((fails + 1)); fi
}
run python -c "$setup" /w/policy.json
expect "렌더링한 정책: 쓰기 · 읽기 신원 허용 · 거부가 의도대로" PASS
run python -c "$setup" /w/weak.json --existing
expect "삭제 거부 문을 뺀 정책은 잡힌다" FAIL
echo "실패 ${fails}건"
exit $((fails > 0))
