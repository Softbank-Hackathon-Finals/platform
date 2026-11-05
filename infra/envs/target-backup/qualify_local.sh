#!/usr/bin/env bash
# render.py의 신원 · 버킷 정책을 실제 SeaweedFS 4.52(S3 TLS)에 넣고 verify.py로 잰다. 일회용 키 · 내부 네트워크 ·
# 공개 포트 없음. 끝나면 컨테이너 · 이미지 · 키를 지운다.
#  1. 렌더링한 정책 + 앱 · Executor(쓰기 권한을 준 합성 신원) + --disposable: PASS
#  2. 삭제 거부 문을 뺀 정책: FAIL, 시험 객체를 쓰기 전에 멈춤(정책이 렌더링과 다름)
#  3. 정책 없음: FAIL, 시험 객체를 쓰지 않음(보호를 읽지 못하면 변경성 시험에 들어가지 않는다)
#  4. 쓰기 키 자리에 관리자 키(잘못된 자격), 등록 버킷 모드: 끝난 뒤 버전 관리 · 정책 그대로
#  5. 앱 · Executor 신원 없이: UNKNOWN, 미확인 목록에 app · executor
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
keys = {who: (secrets.token_hex(10).upper(), secrets.token_hex(20)) for who in ("ADMIN", "WRITER", "READER", "APP", "EXECUTOR")}
cfg = [{"name": i["name"], "credentials": [{"accessKey": keys[names[i["name"]]][0], "secretKey": keys[names[i["name"]]][1]}],
        "actions": i["actions"]} for i in ids]
extra = {"admin": ("ADMIN", ["Admin:bravo-backup", "Read:bravo-backup", "Write:bravo-backup", "List:bravo-backup"]),
         # like the attachment client: rights on its own bucket only
         "app": ("APP", ["Read:demo-chat-attachments", "Write:demo-chat-attachments", "List:demo-chat-attachments"]),
         # review case: an Executor that was given Write on the backup bucket
         "executor": ("EXECUTOR", ["Write:bravo-backup", "List:bravo-backup"])}
cfg += [{"name": n, "credentials": [{"accessKey": keys[w][0], "secretKey": keys[w][1]}], "actions": a} for n, (w, a) in extra.items()]
json.dump({"identities": cfg}, open(f"{w}/s3.json", "w"))
open(f"{w}/keys.env", "w").write("".join(f"T35_{who}_AK={k[0]}\nT35_{who}_SK={k[1]}\n" for who, k in keys.items()))
# wrong credentials: the writer slot holds the owner's keys
open(f"{w}/wrong.env", "w").write("".join(f"T35_{who}_AK={keys['ADMIN' if who == 'WRITER' else who][0]}\nT35_{who}_SK={keys['ADMIN' if who == 'WRITER' else who][1]}\n" for who in keys))
weak = json.load(open(f"{w}/policy.json"))
weak["Statement"] = [s for s in weak["Statement"] if s["Sid"] != "NobodyDeletesBackups"]
json.dump(weak, open(f"{w}/weak.json", "w"))
PY
chmod -R a+rX "$W"
docker network create --internal "$N" >/dev/null
docker run -d --platform linux/amd64 --name "$N-s3" --network "$N" --network-alias seaweedfs \
  -v "$W/s3.json:/etc/s3.json:ro" -v "$W/tls.crt:/tls/tls.crt:ro" -v "$W/tls.key:/tls/tls.key:ro" "$SW" \
  server -dir=/data -s3 -s3.config=/etc/s3.json -s3.iam=false -s3.cert.file=/tls/tls.crt -s3.key.file=/tls/tls.key \
  -s3.port.iceberg=0 -s3.port.lance=0 >/dev/null
run() { local env=$1; shift; docker run --rm -i --network "$N" --env-file "$W/$env" -v "$W:/w:ro" "$N-cr" "$@"; }
# 관리자(버킷 소유자)가 버킷 · 버전 관리를 만들고 정책을 넣거나(파일 인자) 내린다(none).
admin='import json, os, sys, time
import botocore.session
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError
c = botocore.session.get_session().create_client("s3", region_name="us-east-1", endpoint_url="https://seaweedfs:8333", verify="/w/ca.crt",
    aws_access_key_id=os.environ["T35_ADMIN_AK"], aws_secret_access_key=os.environ["T35_ADMIN_SK"], config=Config(s3={"addressing_style": "path"}))
if sys.argv[1] == "state":
    v = c.get_bucket_versioning(Bucket="bravo-backup").get("Status")
    try:
        p = json.loads(c.get_bucket_policy(Bucket="bravo-backup")["Policy"])
    except ClientError:
        p = None
    print(json.dumps({"versioning": v, "policy_sids": sorted(s["Sid"] for s in p["Statement"]) if p else None}))
    sys.exit()
for _ in range(30):
    try:
        c.head_bucket(Bucket="bravo-backup"); break
    except EndpointConnectionError:
        time.sleep(2)
    except ClientError:
        c.create_bucket(Bucket="bravo-backup"); break
try:
    c.delete_bucket_policy(Bucket="bravo-backup")  # the policy denies versioning changes to everyone, owner included
except ClientError as e:
    if e.response["Error"]["Code"] != "NoSuchBucketPolicy":
        raise
c.put_bucket_versioning(Bucket="bravo-backup", VersioningConfiguration={"Status": "Enabled"})
if sys.argv[1] != "none":
    c.put_bucket_policy(Bucket="bravo-backup", Policy=open(sys.argv[1]).read())'
fails=0
check() { if [ "$1" = ok ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails + 1)); fi; }
verify() { run "$1" python verify.py --endpoint https://seaweedfs:8333 --ca /w/ca.crt "${@:2}" || true; }
field() { python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(eval(sys.argv[1], {}, {"d": d}))' "$1"; }

run keys.env python -c "$admin" /w/policy.json
out=$(verify keys.env --deny app --deny executor --disposable)
[ "$(echo "$out" | field 'd["status"]')" = PASS ] && r=ok || r=fail
check $r "렌더링한 정책, 앱 · Executor 포함, 폐기 버킷 모드: PASS (Executor 삭제 $(echo "$out" | field 'd.get("executor.delete_version")'), 정책 변경 $(echo "$out" | field 'd.get("executor.delete_policy")'))"

run keys.env python -c "$admin" /w/weak.json
out=$(verify keys.env --deny app --deny executor)
[ "$(echo "$out" | field 'd["status"]')" = FAIL ] && r=ok || r=fail
check $r "삭제 거부 문을 뺀 정책: FAIL, 시험 객체를 쓰기 전에 멈춤 (policy_matches $(echo "$out" | field 'd.get("policy_matches")'), 시험 객체 $(echo "$out" | field '"writer.put" in d'))"

run keys.env python -c "$admin" none
out=$(verify keys.env --deny app --deny executor)
[ "$(echo "$out" | field 'd["status"]')" = FAIL ] && [ "$(echo "$out" | field '"writer.put" in d')" = False ] && r=ok || r=fail
check $r "정책 없음: FAIL, 시험 객체를 쓰지 않음 (policy_matches $(echo "$out" | field 'd.get("policy_matches")'))"

run keys.env python -c "$admin" /w/policy.json
before=$(run keys.env python -c "$admin" state)
verify wrong.env --deny app --deny executor >/dev/null
after=$(run keys.env python -c "$admin" state)
[ "$before" = "$after" ] && [ "$(echo "$after" | field 'd["versioning"]')" = Enabled ] && r=ok || r=fail
check $r "쓰기 자리에 관리자 키, 등록 버킷 모드: 버전 관리 · 정책 그대로 ($after)"

out=$(verify keys.env)
[ "$(echo "$out" | field 'd["status"]')" = UNKNOWN ] && [ "$(echo "$out" | field 'd["unverified"]')" = "['app', 'executor']" ] && r=ok || r=fail
check $r "앱 · Executor 신원 없이: UNKNOWN, 미확인 $(echo "$out" | field 'd["unverified"]')"
echo "실패 ${fails}건"
exit $((fails > 0))
